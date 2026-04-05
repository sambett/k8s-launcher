"""
routes/gpu_policy.py — GPU quota policy management via Kyverno ClusterPolicies.

Endpoints
---------
GET  /api/gpu-policy                  — read current rules from ConfigMap
POST /api/gpu-policy                  — validate + save rules + apply ClusterPolicy
GET  /api/gpu-policy/status           — is the ClusterPolicy applied and ready?
GET  /api/gpu-policy/available-groups — groups that have at least one JupyterHub profile
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

POLICY_CM_NAME      = "gpu-group-policy"
POLICY_CM_NS        = config.CONFIGMAP_NS        # "jhub"
KYVERNO_POLICY_NAME = "jupyterhub-gpu-group-policy"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _run(cmd):
    """Run a subprocess command list. Returns CompletedProcess."""
    return subprocess.run(cmd, capture_output=True, text=True)


def _read_policy():
    """Return the rules dict from the gpu-group-policy ConfigMap, or empty dict."""
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
    Persist rules dict to the gpu-group-policy ConfigMap.
    Creates the ConfigMap if it does not exist yet.
    Returns (success: bool, detail: str).
    """
    json_str = json.dumps(data, indent=2)

    check = _run(["kubectl", "get", "configmap", POLICY_CM_NAME, "-n", POLICY_CM_NS])

    if check.returncode != 0:
        r = _run([
            "kubectl", "create", "configmap", POLICY_CM_NAME,
            "-n", POLICY_CM_NS,
            "--from-literal", "policy=" + json_str
        ])
        return r.returncode == 0, r.stdout + r.stderr

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
    Build a Kyverno ClusterPolicy dict and serialise it as YAML.

    One rule is generated per entry in groups. Multiple rules for the same
    group are intentional and valid — each targets a different GPU type
    or enforces a different limit. Kyverno evaluates all matching rules;
    the most restrictive one wins.

    Rule names use the enumerate index so they are always unique even when
    the same group appears multiple times.
    """
    rules = []
    for i, g in enumerate(groups):
        gname    = g["gitlab_group"]
        gpu_type = g.get("gpu_type", "nvidia.com/gpu")
        max_gpus = g.get("max_gpus", 0)
        safe     = gname.replace("/", "-").replace("_", "-").lower()

        # JMESPath: gpu_type key contains a dot so it must be double-quoted.
        # PyYAML serialises Python strings containing both " and ' using
        # double-quoted YAML scalar style, which kubectl handles correctly.
        key_expr = (
            "{{ request.object.spec.containers[0]"
            ".resources.limits." + '"' + gpu_type + '"' + " || '0' }}"
        )

        rules.append({
            "name": "limit-gpu-" + safe + "-" + str(i),
            "match": {
                "any": [{
                    "resources": {
                        "kinds":      ["Pod"],
                        "namespaces": [POLICY_CM_NS],
                        "selector": {
                            "matchLabels": {
                                "workbench/gitlab-group": gname
                            }
                        }
                    }
                }]
            },
            "validate": {
                "message": (
                    "Group '" + gname + "' may use at most " +
                    str(max_gpus) + " GPU(s) of type " + gpu_type + "."
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


# ── Endpoints ──────────────────────────────────────────────────────────────────

@bp.route("/api/gpu-policy")
@require_auth
def api_get_policy():
    return jsonify(_read_policy())


@bp.route("/api/gpu-policy", methods=["POST"])
@require_auth
def api_set_policy():
    """
    Save and apply GPU quota rules.

    Multiple rules for the same gitlab_group are intentionally allowed so an
    admin can set different limits for different GPU types (e.g. nvidia.com/gpu
    vs nvidia.com/mig-1g.5gb). No server-side deduplication is performed.
    """
    data   = request.json or {}
    groups = data.get("groups", [])

    for g in groups:
        if not str(g.get("gitlab_group", "")).strip():
            return jsonify({"success": False,
                            "error": "Each rule needs a non-empty gitlab_group"}), 400
        if not isinstance(g.get("max_gpus", 0), int) or g.get("max_gpus", 0) < 0:
            return jsonify({"success": False,
                            "error": "max_gpus must be a non-negative integer"}), 400

    # Strip whitespace from group names but do NOT deduplicate —
    # multiple rules per group with different gpu_type values are valid.
    for g in groups:
        g["gitlab_group"] = g["gitlab_group"].strip()

    ok, detail = _write_policy({"groups": groups})
    if not ok:
        return jsonify({
            "success": False,
            "error":   "Failed to save rules to ConfigMap",
            "detail":  detail
        }), 500

    if not groups:
        r = _run(["kubectl", "delete", "clusterpolicy",
                  KYVERNO_POLICY_NAME, "--ignore-not-found"])
        return jsonify({
            "success": True,
            "message": "All rules cleared — ClusterPolicy removed.",
            "detail":  r.stdout
        })

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
            "message": str(len(groups)) + " rule(s) applied to cluster.",
            "detail":  r.stdout
        })

    return jsonify({
        "success": False,
        "error":   "kubectl apply failed — is Kyverno installed?",
        "detail":  r.stderr or r.stdout
    }), 500


@bp.route("/api/gpu-policy/status")
@require_auth
def api_policy_status():
    """
    Two-step status check.
    Step 1: does the ClusterPolicy exist?
    Step 2: is its .status.ready field true?

    Avoids the jsonpath filter predicate [?(@.type=='Ready')] which is not
    reliably supported across all kubectl patch versions.
    """
    exists = _run([
        "kubectl", "get", "clusterpolicy", KYVERNO_POLICY_NAME, "--no-headers"
    ])
    if exists.returncode != 0:
        return jsonify({"status": "not_applied", "ready": False})

    ready_r = _run([
        "kubectl", "get", "clusterpolicy", KYVERNO_POLICY_NAME,
        "-o", "jsonpath={.status.ready}"
    ])
    ready = (ready_r.returncode == 0 and
             ready_r.stdout.strip().lower() == "true")

    return jsonify({"status": "applied", "ready": ready})


@bp.route("/api/gpu-policy/available-groups")
@require_auth
def api_available_groups():
    """
    Return unique group paths from the jupyterhub-profiles ConfigMap.
    These are the only groups that should have GPU quota rules — every group
    that has at least one profile is a valid rule candidate.
    """
    r = _run([
        "kubectl", "get", "configmap", config.CONFIGMAP_NAME,
        "-n", config.CONFIGMAP_NS,
        "-o", r"jsonpath={.data.profiles\.json}"
    ])

    if r.returncode != 0:
        return jsonify({
            "groups": [],
            "error": "Could not read jupyterhub-profiles ConfigMap"
        })

    raw = r.stdout.strip()
    if not raw:
        return jsonify({"groups": []})

    try:
        profiles = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return jsonify({"groups": [], "error": "profiles.json is not valid JSON"})

    seen = []
    for p in profiles:
        g = str(p.get("gitlab_group", "")).strip()
        if g and g not in seen:
            seen.append(g)

    return jsonify({"groups": seen})
