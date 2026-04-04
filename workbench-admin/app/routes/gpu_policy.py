"""
routes/gpu_policy.py — GPU quota policy management via Kyverno ClusterPolicies.
"""
import json, os, subprocess, tempfile
import yaml
from flask import Blueprint, jsonify, request
from app.auth import require_auth
import app.config as config

bp = Blueprint("gpu_policy", __name__)

POLICY_CM_NAME      = "gpu-group-policy"
POLICY_CM_NS        = config.CONFIGMAP_NS
KYVERNO_POLICY_NAME = "jupyterhub-gpu-group-policy"


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _read_policy():
    r = _run(["kubectl", "get", "configmap", POLICY_CM_NAME,
              "-n", POLICY_CM_NS, "-o", "jsonpath={.data.policy}"])
    if r.returncode != 0:
        return {"groups": []}
    try:
        return json.loads(r.stdout.strip())
    except Exception:
        return {"groups": []}


def _write_policy(data):
    json_str = json.dumps(data, indent=2)
    check = _run(["kubectl", "get", "configmap", POLICY_CM_NAME, "-n", POLICY_CM_NS])
    if check.returncode != 0:
        r = _run(["kubectl", "create", "configmap", POLICY_CM_NAME,
                  "-n", POLICY_CM_NS, "--from-literal", f"policy={json_str}"])
        return r.returncode == 0, r.stdout + r.stderr
    patch = {"data": {"policy": json_str}}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(patch, f)
        pf = f.name
    r = _run(["kubectl", "patch", "configmap", POLICY_CM_NAME,
              "-n", POLICY_CM_NS, "--type=merge", "--patch-file", pf])
    os.unlink(pf)
    return r.returncode == 0, r.stdout + r.stderr


def _build_cluster_policy(groups):
    rules = []
    for i, g in enumerate(groups):
        gname    = g["gitlab_group"]
        gpu_type = g.get("gpu_type", "nvidia.com/gpu")
        max_gpus = g.get("max_gpus", 0)
        safe     = gname.replace("/", "-").replace("_", "-").lower()
        key_expr = (
            "{{ request.object.spec.containers[0]"
            ".resources.limits." + f'"{gpu_type}"' + " || '0' }}"
        )
        rules.append({
            "name": f"limit-gpu-{safe}-{i}",
            "match": {"any": [{"resources": {
                "kinds": ["Pod"], "namespaces": [POLICY_CM_NS],
                "selector": {"matchLabels": {"jupyterhub.io/gitlab-group": gname}}
            }}]},
            "validate": {
                "message": f"Group {gname!r} may use at most {max_gpus} GPU(s) of type {gpu_type}.",
                "deny": {"conditions": {"any": [{
                    "key": key_expr,
                    "operator": "GreaterThan",
                    "value": str(max_gpus)
                }]}}
            }
        })
    policy = {
        "apiVersion": "kyverno.io/v1",
        "kind": "ClusterPolicy",
        "metadata": {
            "name": KYVERNO_POLICY_NAME,
            "annotations": {
                "policies.kyverno.io/title": "JupyterHub GPU Group Quota",
                "policies.kyverno.io/description": "Per-GitLab-group GPU limits on JupyterHub pods."
            }
        },
        "spec": {
            "validationFailureAction": "Enforce",
            "background": False,
            "rules": rules
        }
    }
    return yaml.dump(policy, default_flow_style=False, allow_unicode=True)


@bp.route("/api/gpu-policy")
@require_auth
def api_get_policy():
    return jsonify(_read_policy())


@bp.route("/api/gpu-policy", methods=["POST"])
@require_auth
def api_set_policy():
    data   = request.json or {}
    groups = data.get("groups", [])
    for g in groups:
        if not g.get("gitlab_group", "").strip():
            return jsonify({"success": False, "error": "Each rule needs a gitlab_group"}), 400
        if not isinstance(g.get("max_gpus", 0), int) or g.get("max_gpus", 0) < 0:
            return jsonify({"success": False, "error": "max_gpus must be a non-negative integer"}), 400
    ok, out = _write_policy({"groups": groups})
    if not ok:
        return jsonify({"success": False, "error": "Failed to save to ConfigMap", "detail": out}), 500
    if not groups:
        r = _run(["kubectl", "delete", "clusterpolicy", KYVERNO_POLICY_NAME, "--ignore-not-found"])
        return jsonify({"success": True, "message": "Policy cleared", "detail": r.stdout})
    policy_yaml = _build_cluster_policy(groups)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(policy_yaml)
        pf = f.name
    r = _run(["kubectl", "apply", "-f", pf])
    os.unlink(pf)
    if r.returncode == 0:
        return jsonify({"success": True,
                        "message": f"ClusterPolicy applied ({len(groups)} rule(s))",
                        "detail": r.stdout})
    return jsonify({"success": False, "error": "kubectl apply failed", "detail": r.stderr}), 500


@bp.route("/api/gpu-policy/status")
@require_auth
def api_policy_status():
    r = _run(["kubectl", "get", "clusterpolicy", KYVERNO_POLICY_NAME,
              "-o", "jsonpath={.status.conditions[?(@.type==" + chr(39) + "Ready" + chr(39) + ")].status}"])
    if r.returncode != 0:
        return jsonify({"status": "not_applied", "ready": False})
    return jsonify({"status": "applied", "ready": r.stdout.strip().lower() == "true"})


@bp.route("/api/gpu-policy/available-groups")
@require_auth
def api_available_groups():
    r = _run(["kubectl", "get", "configmap", config.CONFIGMAP_NAME,
              "-n", config.CONFIGMAP_NS, "-o", "jsonpath={.data.profiles\.json}"])
    if r.returncode != 0:
        return jsonify({"groups": []})
    try:
        profiles = json.loads(r.stdout.strip())
        seen = []
        for p in profiles:
            g = p.get("gitlab_group", "").strip()
            if g and g not in seen:
                seen.append(g)
        return jsonify({"groups": seen})
    except Exception:
        return jsonify({"groups": []})
