"""
routes/gpu_policy.py — GPU access policy management via Kyverno ClusterPolicies.

Three-policy architecture:
  Policy 1 (static, Ansible):   require-gpu-node-selector
    Any pod requesting GPU must declare nodeSelector.nvidia.com/gpu.product

  Policy 2 (dynamic, this file): jupyterhub-gpu-block
    Groups explicitly blocked from ALL GPU access.
    One ClusterPolicy, one rule, all blocked groups in the match list.

  Policy 3 (dynamic, this file): jupyterhub-gpu-types
    Groups with a GPU type allowlist — one rule per group inside one policy.
    Pod requesting a type not in the group's allowlist is denied.

Schema stored in ConfigMap gpu-group-policy:
  {
    "blocked_groups": ["internship", "temp-users"],
    "type_policies": [
      {"gitlab_group": "data-science", "allowed_gpu_types": ["NVIDIA-A2"]},
      {"gitlab_group": "admins",       "allowed_gpu_types": ["NVIDIA-A2", "NVIDIA-H100"]}
    ]
  }

Groups not in either list = unrestricted. No Kyverno rule generated for them.
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

POLICY_CM_NAME    = "gpu-group-policy"
POLICY_CM_NS      = config.CONFIGMAP_NS
BLOCK_POLICY_NAME = "jupyterhub-gpu-block"
TYPES_POLICY_NAME = "jupyterhub-gpu-types"

_KUBECTL = shutil.which("kubectl") or "/usr/local/bin/kubectl"

# Dot notation — bracket notation ["nvidia.com/gpu"] rejected by Kyverno webhook
# PyYAML will serialize these with escaped double quotes, which YAML parsers
# correctly unescape back to: ."nvidia.com/gpu" (dot + quoted identifier)
_GPU_COUNT_EXPR   = '{{ request.object.spec.containers[0].resources.limits."nvidia.com/gpu" || \'0\' }}'
_GPU_PRODUCT_EXPR = '{{ request.object.spec.nodeSelector."nvidia.com/gpu.product" || \'\' }}'


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _apply_policy(policy_yaml, policy_name):
    """Delete then create — avoids resourceVersion conflict on Kyverno CRD objects."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(policy_yaml)
        policy_file = f.name
    try:
        _run([_KUBECTL, "delete", "clusterpolicy", policy_name, "--ignore-not-found"])
        r = _run([_KUBECTL, "create", "-f", policy_file])
        return r.returncode == 0, r.stdout + r.stderr
    finally:
        os.unlink(policy_file)


def _delete_policy(policy_name):
    _run([_KUBECTL, "delete", "clusterpolicy", policy_name, "--ignore-not-found"])


def _read_policy():
    r = _run([
        _KUBECTL, "get", "configmap", POLICY_CM_NAME,
        "-n", POLICY_CM_NS, "-o", "jsonpath={.data.policy}"
    ])
    if r.returncode != 0 or not r.stdout.strip():
        return {"blocked_groups": [], "type_policies": []}
    try:
        data = json.loads(r.stdout.strip())
        data.setdefault("blocked_groups", [])
        data.setdefault("type_policies",  [])
        return data
    except (json.JSONDecodeError, ValueError):
        return {"blocked_groups": [], "type_policies": []}


def _write_policy(data):
    json_str = json.dumps(data, indent=2)
    check = _run([_KUBECTL, "get", "configmap", POLICY_CM_NAME, "-n", POLICY_CM_NS])
    if check.returncode != 0:
        r = _run([
            _KUBECTL, "create", "configmap", POLICY_CM_NAME,
            "-n", POLICY_CM_NS, "--from-literal", "policy=" + json_str
        ])
        return r.returncode == 0, r.stdout + r.stderr
    patch = {"data": {"policy": json_str}}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(patch, f)
        pf = f.name
    try:
        r = _run([
            _KUBECTL, "patch", "configmap", POLICY_CM_NAME,
            "-n", POLICY_CM_NS, "--type=merge", "--patch-file", pf
        ])
        return r.returncode == 0, r.stdout + r.stderr
    finally:
        os.unlink(pf)


def _rule_name(prefix, gitlab_group):
    safe  = gitlab_group.replace("/", "-").replace("_", "-").lower()
    token = hashlib.md5(gitlab_group.encode()).hexdigest()[:6]
    return f"{prefix}-{safe}-{token}"


def _build_block_policy(blocked_groups):
    """
    One ClusterPolicy, one rule, all blocked groups in the match list.
    Deny fires when the pod requests nvidia.com/gpu > 0.
    CPU pods from blocked groups are unaffected.
    """
    if not blocked_groups:
        return None
    policy = {
        "apiVersion": "kyverno.io/v1",
        "kind":       "ClusterPolicy",
        "metadata": {
            "name": BLOCK_POLICY_NAME,
            "annotations": {
                "policies.kyverno.io/title":
                    "JupyterHub GPU Block Policy",
                "policies.kyverno.io/description":
                    "Denies GPU pod spawning for groups with no GPU access."
            }
        },
        "spec": {
            "validationFailureAction": "Enforce",
            "background":             False,
            "rules": [{
                "name": "deny-blocked-groups",
                "match": {
                    "any": [
                        {
                            "resources": {
                                "kinds":      ["Pod"],
                                "namespaces": [POLICY_CM_NS],
                                "operations": ["CREATE"],
                                "selector": {
                                    "matchLabels": {
                                        "workbench/gitlab-group": grp
                                    }
                                }
                            }
                        }
                        for grp in blocked_groups
                    ]
                },
                "validate": {
                    "message": (
                        "Your group does not have GPU access. "
                        "Contact your administrator to request GPU permissions."
                    ),
                    "deny": {
                        "conditions": {
                            "any": [{
                                "key":      _GPU_COUNT_EXPR,
                                "operator": "GreaterThan",
                                "value":    "0"
                            }]
                        }
                    }
                }
            }]
        }
    }
    return yaml.dump(policy, default_flow_style=False, allow_unicode=True)

def _build_types_policy(type_policies):
    """
    One ClusterPolicy, one rule per group with an allowlist.
    Precondition: pod requests GPU (count > 0).
    Deny fires when nodeSelector.nvidia.com/gpu.product NOT IN allowed list.
    Groups with an empty allowlist are skipped (should be in blocked_groups instead).
    """
    rules = [
        {
            "name": _rule_name("gpu-type", tp["gitlab_group"]),
            "match": {
                "any": [{
                    "resources": {
                        "kinds":      ["Pod"],
                        "namespaces": [POLICY_CM_NS],
                        "operations": ["CREATE"],
                        "selector": {
                            "matchLabels": {
                                "workbench/gitlab-group": tp["gitlab_group"]
                            }
                        }
                    }
                }]
            },
            "preconditions": {
                "all": [{
                    "key":      _GPU_COUNT_EXPR,
                    "operator": "GreaterThan",
                    "value":    "0"
                }]
            },
            "validate": {
                "message": (
                    f"Group '{tp['gitlab_group']}' may only use: "
                    f"{', '.join(tp['allowed_gpu_types'])}."
                ),
                "deny": {
                    "conditions": {
                        "all": [{
                            "key":      _GPU_PRODUCT_EXPR,
                            "operator": "NotIn",
                            "value":    tp["allowed_gpu_types"]
                        }]
                    }
                }
            }
        }
        for tp in type_policies
        if tp.get("allowed_gpu_types")
    ]
    if not rules:
        return None
    policy = {
        "apiVersion": "kyverno.io/v1",
        "kind":       "ClusterPolicy",
        "metadata": {
            "name": TYPES_POLICY_NAME,
            "annotations": {
                "policies.kyverno.io/title":
                    "JupyterHub GPU Type Policy",
                "policies.kyverno.io/description":
                    "Enforces GPU type allowlists per group."
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
    data           = request.json or {}
    blocked_groups = [str(g).strip() for g in data.get("blocked_groups", []) if str(g).strip()]
    type_policies  = data.get("type_policies", [])

    seen = set()
    for tp in type_policies:
        grp = str(tp.get("gitlab_group", "")).strip()
        if not grp:
            return jsonify({"success": False, "error": "Each type policy needs a gitlab_group"}), 400
        if grp in seen:
            return jsonify({"success": False, "error": f"Duplicate group '{grp}' in type policies"}), 400
        if grp in blocked_groups:
            return jsonify({"success": False, "error": f"'{grp}' is in both blocked and type policies"}), 400
        if not isinstance(tp.get("allowed_gpu_types", []), list):
            return jsonify({"success": False, "error": f"allowed_gpu_types must be a list for '{grp}'"}), 400
        seen.add(grp)
        tp["gitlab_group"] = grp

    ok, detail = _write_policy({"blocked_groups": blocked_groups, "type_policies": type_policies})
    if not ok:
        return jsonify({"success": False, "error": "Failed to save to ConfigMap", "detail": detail}), 500

    results = {}

    block_yaml = _build_block_policy(blocked_groups)
    if block_yaml:
        ok, detail = _apply_policy(block_yaml, BLOCK_POLICY_NAME)
        results["block"] = "applied" if ok else f"failed: {detail}"
    else:
        _delete_policy(BLOCK_POLICY_NAME)
        results["block"] = "removed"

    types_yaml = _build_types_policy(type_policies)
    if types_yaml:
        ok, detail = _apply_policy(types_yaml, TYPES_POLICY_NAME)
        results["types"] = "applied" if ok else f"failed: {detail}"
    else:
        _delete_policy(TYPES_POLICY_NAME)
        results["types"] = "removed"

    if any("failed" in v for v in results.values()):
        return jsonify({"success": False, "error": "Policy apply failed", "detail": results}), 500

    restricted = len([t for t in type_policies if t.get("allowed_gpu_types")])
    return jsonify({
        "success": True,
        "message": (
            f"{len(blocked_groups)} group(s) blocked · "
            f"{restricted} group(s) with type restrictions."
        ),
        "detail": results
    })


@bp.route("/api/gpu-policy/status")
@require_auth
def api_policy_status():
    def _check(name):
        r = _run([_KUBECTL, "get", "clusterpolicy", name, "--no-headers"])
        if r.returncode != 0:
            return {"exists": False, "ready": False}
        rr = _run([_KUBECTL, "get", "clusterpolicy", name,
                   "-o", "jsonpath={.status.ready}"])
        return {"exists": True, "ready": rr.returncode == 0 and rr.stdout.strip().lower() == "true"}

    return jsonify({
        "block_policy": _check(BLOCK_POLICY_NAME),
        "types_policy": _check(TYPES_POLICY_NAME),
    })


@bp.route("/api/gpu-policy/available-groups")
@require_auth
def api_available_groups():
    r = _run([
        _KUBECTL, "get", "configmap", config.CONFIGMAP_NAME,
        "-n", config.CONFIGMAP_NS,
        "-o", r"jsonpath={.data.profiles\.json}"
    ])
    if r.returncode != 0:
        return jsonify({"groups": [], "error": "Could not read jupyterhub-profiles ConfigMap"})
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
