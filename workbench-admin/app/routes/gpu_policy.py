"""
routes/gpu_policy.py — GPU quota policy management via Kyverno ClusterPolicies.

Responsibilities
────────────────
• Persist per-group GPU rules in a dedicated ConfigMap (gpu-group-policy).
• Generate a Kyverno ClusterPolicy YAML from those rules and apply it via kubectl.
• Expose a read-only status endpoint for the UI badge.
• Expose an available-groups endpoint so the UI dropdown shows every group
  that has a JupyterHub profile (not just those already in a rule).

All kubectl calls run in-process via subprocess.run() with a list argv
(never shell=True) so there is no shell-injection risk.
"""

import json
import os
import subprocess
import tempfile

import yaml
from flask import Blueprint, jsonify, request

from app.auth import require_auth
import app.config as config

bp = Blueprint("gpu_policy", __name__)

# ── Constants ──────────────────────────────────────────────────────────────────
POLICY_CM_NAME      = "gpu-group-policy"
POLICY_CM_NS        = config.CONFIGMAP_NS        # "jhub"
KYVERNO_POLICY_NAME = "jupyterhub-gpu-group-policy"


# ── Internal helpers ────────────────────────────────────────────────────────────

def _run(cmd):
    """Run a kubectl command. Returns CompletedProcess (check .returncode / .stdout / .stderr)."""
    return subprocess.run(cmd, capture_output=True, text=True)


def _read_policy():
    """Return the current rules dict from the gpu-group-policy ConfigMap, or an empty one."""
    r = _run([
        "kubectl", "get", "configmap", POLICY_CM_NAME,
        "-n", POLICY_CM_NS, "-o", "jsonpath={.data.policy}"
    ])
    if r.returncode != 0:
        return {"groups": []}
    try:
        return json.loads(r.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return {"groups": []}


def _write_policy(data):
    """
    Persist the rules dict to the gpu-group-policy ConfigMap.
    Creates the ConfigMap if it does not exist yet.
    Returns (success: bool, detail: str).
    """
    json_str = json.dumps(data, indent=2)

    # Check whether the ConfigMap already exists
    check = _run(["kubectl", "get", "configmap", POLICY_CM_NAME, "-n", POLICY_CM_NS])

    if check.returncode != 0:
        # Create new
        r = _run([
            "kubectl", "create", "configmap", POLICY_CM_NAME,
            "-n", POLICY_CM_NS,
            "--from-literal", "policy=" + json_str
        ])
        return r.returncode == 0, r.stdout + r.stderr

    # Patch existing
    patch = {"data": {"policy": json_str}}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(patch, f)
        patch_file = f.name

    try:
        r = _run([
            "kubectl", "patch", "configmap", POLICY_CM_NAME,
            "-n", POLICY_CM_NS,
            "--type=merge",
            "--patch-file", patch_file
        ])
    finally:
        os.unlink(patch_file)

    return r.returncode == 0, r.stdout + r.stderr


def _build_cluster_policy(groups):
    """
    Build a Kyverno ClusterPolicy dict from the rules list and serialise it as YAML.

    One Kyverno validate rule is generated per group.  The rule denies any Pod in
    the jhub namespace whose `workbench/gitlab-group` label matches the group when
    the requested GPU count exceeds max_gpus.

    We use PyYAML's yaml.dump() rather than a hand-written template because it
    correctly handles quoting of the JMESPath expression (which contains both
    double-quotes and curly braces) without the risk of manual escaping mistakes.
    """
    rules = []
    for i, g in enumerate(groups):
        gname    = g["gitlab_group"]
        gpu_type = g.get("gpu_type", "nvidia.com/gpu")
        max_gpus = g.get("max_gpus", 0)
        # Produce a DNS-safe rule name
        safe     = gname.replace("/", "-").replace("_", "-").lower()

        # Kyverno JMESPath: the gpu_type key contains a dot, so it must be
        # quoted with double-quotes inside the expression string.
        # PyYAML will serialise the Python string that contains " and ' using
        # double-quoted YAML scalar style, which is exactly what kubectl expects.
        key_expr = (
            "{{ request.object.spec.containers[0]"
            ".resources.limits." + f'"{gpu_type}"' + " || '0' }}"
        )

        rules.append({
            "name": f"limit-gpu-{safe}-{i}",
            "match": {
                "any": [{
                    "resources": {
                        "kinds": ["Pod"],
                        "namespaces": [POLICY_CM_NS],
                        "selector": {
                            "matchLabels": {
                                # Must match the label stamped by KubeSpawner's extra_labels
                                "workbench/gitlab-group": gname
                            }
                        }
                    }
                }]
            },
            "validate": {
                "message": (
                    f"Group '{gname}' may use at most {max_gpus} "
                    f"GPU(s) of type {gpu_type}."
                ),
                "deny": {
                    "conditions": {
                        "any": [{
                            "key":      key_expr,
                            "operator": "GreaterThan",
                            "value":    str(max_gpus)
                        }]
                    }
                }
            }
        })

    policy = {
        "apiVersion": "kyverno.io/v1",
        "kind":       "ClusterPolicy",
        "metadata": {
            "name": KYVERNO_POLICY_NAME,
            "annotations": {
                "policies.kyverno.io/title":
                    "JupyterHub GPU Group Quota",
                "policies.kyverno.io/description":
                    "Per-GitLab-group GPU limits enforced on JupyterHub notebook pods."
            }
        },
        "spec": {
            "validationFailureAction": "Enforce",
            "background": False,
            "rules": rules
        }
    }

    return yaml.dump(policy, default_flow_style=False, allow_unicode=True)


# ── API endpoints ───────────────────────────────────────────────────────────────

@bp.route("/api/gpu-policy")
@require_auth
def api_get_policy():
    """Return the current rules from the gpu-group-policy ConfigMap."""
    return jsonify(_read_policy())


@bp.route("/api/gpu-policy", methods=["POST"])
@require_auth
def api_set_policy():
    """
    Validate and persist new rules, then apply the Kyverno ClusterPolicy.

    If the groups list is empty the ClusterPolicy is deleted (no quotas enforced).
    Duplicate groups are deduplicated server-side (last entry wins).
    """
    data   = request.json or {}
    groups = data.get("groups", [])

    # Validate inputs
    for g in groups:
        if not str(g.get("gitlab_group", "")).strip():
            return jsonify({"success": False, "error": "Each rule needs a non-empty gitlab_group"}), 400
        if not isinstance(g.get("max_gpus", 0), int) or g.get("max_gpus", 0) < 0:
            return jsonify({"success": False, "error": "max_gpus must be a non-negative integer"}), 400

    # Deduplicate — last row for a given group wins
    seen = {}
    for g in groups:
        seen[g["gitlab_group"].strip()] = g
    groups = list(seen.values())

    # Persist rules to ConfigMap
    ok, detail = _write_policy({"groups": groups})
    if not ok:
        return jsonify({
            "success": False,
            "error":   "Failed to save rules to ConfigMap",
            "detail":  detail
        }), 500

    # If no rules remain, delete the ClusterPolicy (nothing to enforce)
    if not groups:
        r = _run(["kubectl", "delete", "clusterpolicy", KYVERNO_POLICY_NAME, "--ignore-not-found"])
        return jsonify({
            "success": True,
            "message": "All rules cleared — ClusterPolicy removed.",
            "detail":  r.stdout
        })

    # Build and apply the ClusterPolicy YAML
    policy_yaml = _build_cluster_policy(groups)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(policy_yaml)
        policy_file = f.name

    try:
        r = _run(["kubectl", "apply", "-f", policy_file])
    finally:
        os.unlink(policy_file)

    if r.returncode == 0:
        return jsonify({
            "success": True,
            "message": f"ClusterPolicy applied — {len(groups)} rule(s) active.",
            "detail":  r.stdout
        })

    return jsonify({
        "success": False,
        "error":   "kubectl apply failed — check that Kyverno is installed.",
        "detail":  r.stderr or r.stdout
    }), 500


@bp.route("/api/gpu-policy/status")
@require_auth
def api_policy_status():
    """
    Check whether the Kyverno ClusterPolicy exists and is Ready.

    Approach: two kubectl calls so the logic is explicit and testable.
      1. Does the ClusterPolicy exist at all?
      2. Is it in Ready=True state?

    We avoid the jsonpath filter expression [?(@.type=='Ready')] because
    kubectl's jsonpath implementation is a restricted subset and filter
    predicates are not reliable across all patch versions.
    """
    # Step 1 — does the policy exist?
    exist_check = _run([
        "kubectl", "get", "clusterpolicy", KYVERNO_POLICY_NAME, "--no-headers"
    ])

    if exist_check.returncode != 0:
        return jsonify({"status": "not_applied", "ready": False})

    # Step 2 — is it ready? Kyverno exposes .status.ready as a boolean string.
    ready_check = _run([
        "kubectl", "get", "clusterpolicy", KYVERNO_POLICY_NAME,
        "-o", "jsonpath={.status.ready}"
    ])

    ready = (ready_check.returncode == 0 and
             ready_check.stdout.strip().lower() == "true")

    return jsonify({"status": "applied", "ready": ready})


@bp.route("/api/gpu-policy/available-groups")
@require_auth
def api_available_groups():
    """
    Return the unique list of GitLab group paths that appear in the
    jupyterhub-profiles ConfigMap.

    This is the authoritative source for the UI dropdown: every group that has
    at least one JupyterHub profile is a valid candidate for a GPU quota rule.
    """
    r = _run([
        "kubectl", "get", "configmap", config.CONFIGMAP_NAME,
        "-n", config.CONFIGMAP_NS,
        "-o", r"jsonpath={.data.profiles\.json}"
    ])

    if r.returncode != 0:
        # ConfigMap does not exist or kubectl is not reachable
        return jsonify({"groups": [], "error": "Could not read profiles ConfigMap"})

    raw = r.stdout.strip()
    if not raw:
        return jsonify({"groups": []})

    try:
        profiles = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return jsonify({"groups": [], "error": "profiles.json is not valid JSON"})

    # Deduplicate while preserving insertion order
    seen = []
    for p in profiles:
        g = str(p.get("gitlab_group", "")).strip()
        if g and g not in seen:
            seen.append(g)

    return jsonify({"groups": seen})
