"""
routes/gpu_policy.py — GPU access policy management via Kyverno ClusterPolicies.

Policy model (allowlist):
  Each rule defines which GPU types a GitLab group is ALLOWED to use.
  Empty allowed_gpu_types list = group is blocked from all GPU pods.
  Groups not listed = unrestricted (no Kyverno rule generated for them).

Kyverno enforcement:
  For each group with an empty allowlist, one deny rule is generated:
    deny any pod from that group that requests nvidia.com/gpu > 0

  For groups with specific allowed types, no Kyverno rule is needed —
  the nodeSelector in the profile handles physical placement and the
  device plugin enforces resource availability at scheduling time.

Endpoints
---------
GET  /api/gpu-policy                  — read current rules from ConfigMap
POST /api/gpu-policy                  — validate + save rules + apply ClusterPolicy
GET  /api/gpu-policy/status           — is the ClusterPolicy applied and ready?
GET  /api/gpu-policy/available-groups — groups that have at least one JupyterHub profile
"""

import hashlib
import json
import os
import shutil
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

_KUBECTL = shutil.which("kubectl") or "/usr/local/bin/kubectl"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _read_policy():
    """Return the rules dict from the gpu-group-policy ConfigMap, or empty dict."""
    r = _run([
        _KUBECTL, "get", "configmap", POLICY_CM_NAME,
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
    check    = _run([_KUBECTL, "get", "configmap", POLICY_CM_NAME, "-n", POLICY_CM_NS])

    if check.returncode != 0:
        r = _run([
            _KUBECTL, "create", "configmap", POLICY_CM_NAME,
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
            _KUBECTL, "patch", "configmap", POLICY_CM_NAME,
            "-n", POLICY_CM_NS,
            "--type=merge",
            "--patch-file", patch_file
        ])
    finally:
        os.unlink(patch_file)

    return r.returncode == 0, r.stdout + r.stderr


def _stable_rule_name(gitlab_group: str) -> str:
    """
    Stable deny-rule name derived from the group name.
    Uses a 6-char content hash so the name survives reordering.
    Format: deny-gpu-<safe-group>-<hash>
    """
    safe  = gitlab_group.replace("/", "-").replace("_", "-").lower()
    token = hashlib.md5(gitlab_group.encode()).hexdigest()[:6]
    return f"deny-gpu-{safe}-{token}"


def _build_cluster_policy(groups):
    """
    Build a Kyverno ClusterPolicy YAML string from the allowlist rules.

    Logic:
      - Groups with empty allowed_gpu_types → one deny rule generated.
        The rule denies any pod from that group requesting nvidia.com/gpu > 0.
      - Groups with allowed types → NO Kyverno rule needed.
        The nodeSelector in the profile enforces physical placement.
        Kyverno only needs to enforce the "no GPU at all" case.

    This keeps the policy minimal and correct:
      - We never generate rules for groups that are allowed to use GPUs.
      - We only generate rules for groups that are explicitly blocked.
      - Groups not in the policy at all are unrestricted by design.
    """
    rules = []

    for g in groups:
        gname         = g["gitlab_group"]
        allowed_types = g.get("allowed_gpu_types", [])

        # Only generate a deny rule for groups with NO allowed GPU types.
        # Groups with allowed types are handled by nodeSelector + device plugin.
        if allowed_types:
            continue

        # Deny rule: pod from this group with nvidia.com/gpu > 0 is blocked.
        # Uses containers[0] — Kyverno JMESPath scalar operators require a
        # specific container index, not a wildcard array.
        key_expr = (
            '{{ request.object.spec.containers[0].resources.limits.'
            '"nvidia.com/gpu" || \'0\' }}'
        )

        rules.append({
            "name": _stable_rule_name(gname),
            "match": {
                "any": [{
                    "resources": {
                        "kinds":      ["Pod"],
                        "namespaces": [POLICY_CM_NS],
                        "operations": ["CREATE"],
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
                    "Group '" + gname + "' does not have GPU access. "
                    "Contact your administrator to request GPU permissions."
                ),
                "deny": {
                    "conditions": {
                        "any": [{
                            "key":      key_expr,
                            "operator": "GreaterThan",
                            "value":    "0"
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
                    "JupyterHub GPU Group Access",
                "policies.kyverno.io/description":
                    "Blocks GPU pod spawning for groups not granted GPU access."
            }
        },
        "spec": {
            "validationFailureAction": "Enforce",
            "background":             False,
            "rules":                  rules
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
    Save and apply GPU access rules.

    Each rule: { gitlab_group: str, allowed_gpu_types: [str, ...] }
    Empty allowed_gpu_types = group is blocked from all GPU pods.
    Each group may appear only once.
    """
    data   = request.json or {}
    groups = data.get("groups", [])

    # ── Validation ────────────────────────────────────────────────────────────
    seen = set()
    for g in groups:
        grp = str(g.get("gitlab_group", "")).strip()
        if not grp:
            return jsonify({
                "success": False,
                "error":   "Each rule needs a non-empty gitlab_group"
            }), 400
        if grp in seen:
            return jsonify({
                "success": False,
                "error":   f"Duplicate group '{grp}' — each group may appear only once"
            }), 400
        seen.add(grp)
        g["gitlab_group"] = grp

        # allowed_gpu_types must be a list (can be empty)
        if not isinstance(g.get("allowed_gpu_types", []), list):
            return jsonify({
                "success": False,
                "error":   f"allowed_gpu_types must be a list for group '{grp}'"
            }), 400

    ok, detail = _write_policy({"groups": groups})
    if not ok:
        return jsonify({
            "success": False,
            "error":   "Failed to save rules to ConfigMap",
            "detail":  detail
        }), 500

    # If all groups are allowed (no blocked groups), remove the policy entirely
    blocked_groups = [g for g in groups if not g.get("allowed_gpu_types")]

    if not blocked_groups:
        r = _run([
            _KUBECTL, "delete", "clusterpolicy",
            KYVERNO_POLICY_NAME, "--ignore-not-found"
        ])
        msg = (
            f"{len(groups)} rule(s) saved — no blocked groups, ClusterPolicy removed."
            if groups else
            "All rules cleared — ClusterPolicy removed."
        )
        return jsonify({"success": True, "message": msg, "detail": r.stdout})

    policy_yaml = _build_cluster_policy(groups)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(policy_yaml)
        policy_file = f.name
    try:
        # Delete first so kubectl create never hits the resourceVersion conflict
        # that kubectl apply triggers on existing Kyverno CRD objects.
        _run([_KUBECTL, "delete", "clusterpolicy",
              KYVERNO_POLICY_NAME, "--ignore-not-found"])
        r = _run([_KUBECTL, "create", "-f", policy_file])
    finally:
        os.unlink(policy_file)

    if r.returncode == 0:
        return jsonify({
            "success": True,
            "message": (
                f"{len(groups)} group(s) saved — "
                f"{len(blocked_groups)} blocked by Kyverno."
            ),
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
    exists = _run([
        _KUBECTL, "get", "clusterpolicy", KYVERNO_POLICY_NAME, "--no-headers"
    ])
    if exists.returncode != 0:
        return jsonify({"status": "not_applied", "ready": False})

    ready_r = _run([
        _KUBECTL, "get", "clusterpolicy", KYVERNO_POLICY_NAME,
        "-o", "jsonpath={.status.ready}"
    ])
    ready = (
        ready_r.returncode == 0 and
        ready_r.stdout.strip().lower() == "true"
    )
    return jsonify({"status": "applied", "ready": ready})


@bp.route("/api/gpu-policy/available-groups")
@require_auth
def api_available_groups():
    """Return unique group paths from the jupyterhub-profiles ConfigMap."""
    r = _run([
        _KUBECTL, "get", "configmap", config.CONFIGMAP_NAME,
        "-n", config.CONFIGMAP_NS,
        "-o", r"jsonpath={.data.profiles\.json}"
    ])
    if r.returncode != 0:
        return jsonify({
            "groups": [],
            "error":  "Could not read jupyterhub-profiles ConfigMap"
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
