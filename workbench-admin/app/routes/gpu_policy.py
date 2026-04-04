"""
routes/gpu_policy.py — GPU group policy management.

Manages a Kyverno ClusterPolicy that enforces per-GitLab-group GPU quotas.

Flow:
  1. Admin defines group → max_gpus rules in the dashboard UI.
  2. POST /api/gpu-policy saves rules to ConfigMap 'gpu-group-policy' in jhub.
  3. A Kyverno ClusterPolicy is generated from those rules and applied
     with kubectl apply on cp01.
  4. On every pod spawn in jhub, Kyverno checks the pod label
     jupyterhub.io/gitlab-group against the ClusterPolicy.
  5. Pods exceeding the quota are rejected with a clear error message.

Prerequisite (one-time, shown in the UI):
  JupyterHub's KubeSpawner callable must add
  jupyterhub.io/gitlab-group: <group> to every spawned pod.
"""

import json
import os
import subprocess
import tempfile

from flask import Blueprint, jsonify, request
from app.auth import require_auth

bp = Blueprint("gpu_policy", __name__)

# ── Constants ─────────────────────────────────────────────────────────────────

POLICY_CM_NAME   = "gpu-group-policy"
POLICY_CM_NS     = "jhub"
KYVERNO_POLICY   = "jupyterhub-gpu-group-policy"
PROFILES_CM_NAME = "jupyterhub-profiles"

# cp01 is the control plane — kubectl is configured there
CP01_HOST = "cp01"
CP01_USER = "cp01"


# ── Shell helpers ─────────────────────────────────────────────────────────────

def _run_local(cmd: str) -> tuple:
    """Run a command on ansiblectl (where kubectl may or may not be configured)."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip(), r.returncode


def _run_on_cp(cmd: str) -> tuple:
    """Run a kubectl command on cp01 via SSH."""
    ssh_cmd = f'ssh -o StrictHostKeyChecking=no {CP01_USER}@{CP01_HOST} "{cmd}"'
    return _run_local(ssh_cmd)


def _kubectl(cmd: str) -> tuple:
    """
    Run kubectl — try local first (if kubeconfig exists), fall back to cp01 via SSH.
    This makes the code work whether kubectl is configured locally or only on cp01.
    """
    out, rc = _run_local(f"kubectl {cmd} 2>&1")
    if rc != 0 and ("command not found" in out or "Unable to connect" in out):
        return _run_on_cp(f"kubectl {cmd} 2>&1")
    return out, rc


# ── ConfigMap helpers ─────────────────────────────────────────────────────────

def _read_policy() -> list:
    """Return current GPU group rules from the ConfigMap, or [] if none exist."""
    out, rc = _kubectl(
        f"get configmap {POLICY_CM_NAME} -n {POLICY_CM_NS} "
        f"-o jsonpath='{{.data.policy}}' 2>/dev/null"
    )
    if rc != 0 or not out.strip():
        return []
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return []


def _write_policy_cm(groups: list) -> tuple:
    """Persist the group rules as a ConfigMap in the cluster."""
    data = json.dumps(groups, indent=2)
    tmp  = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="gpu-policy-"
    )
    try:
        tmp.write(data)
        tmp.close()
        out, rc = _kubectl(
            f"create configmap {POLICY_CM_NAME} -n {POLICY_CM_NS} "
            f"--from-file=policy={tmp.name} "
            f"--dry-run=client -o yaml | kubectl apply -f - 2>&1"
        )
        # Note: the pipe above only works locally — for cp01 SSH we use a different approach
        if rc != 0:
            # Fallback: pass JSON inline via --from-literal
            escaped = data.replace("'", "'\\''")
            out, rc = _run_on_cp(
                f"kubectl create configmap {POLICY_CM_NAME} -n {POLICY_CM_NS} "
                f"--from-literal=policy='{escaped}' "
                f"--dry-run=client -o yaml | kubectl apply -f -"
            )
        return rc == 0, out
    finally:
        os.unlink(tmp.name)


def _read_available_groups() -> list:
    """Return GitLab groups that have at least one profile in the profiles ConfigMap."""
    out, rc = _kubectl(
        f"get configmap {PROFILES_CM_NAME} -n {POLICY_CM_NS} "
        f"-o jsonpath='{{.data.profiles\\.json}}' 2>/dev/null"
    )
    if rc != 0 or not out.strip():
        return []
    try:
        profiles = json.loads(out)
        return sorted({
            p["gitlab_group"]
            for p in profiles
            if p.get("gitlab_group", "").strip()
        })
    except Exception:
        return []


# ── Kyverno ClusterPolicy generator ──────────────────────────────────────────

def _build_rule(group_name: str, gpu_type: str, max_gpus: int) -> str:
    """Build one Kyverno rule block as YAML text."""
    if max_gpus == 0:
        msg = f"Group '{group_name}' is not permitted to use GPUs on this platform."
    else:
        msg = (
            f"Group '{group_name}' may use at most {max_gpus} "
            f"GPU(s) of type {gpu_type}."
        )

    # JMESPath expression to read the GPU limit from the pod spec.
    # Returns '0' if the field is absent (CPU-only pods pass the check).
    jmes = (
        "{{ request.object.spec.containers[0]"
        ".resources.limits.\"" + gpu_type + "\" || '0' }}"
    )

    return f"""\
  - name: gpu-limit-{group_name}
    match:
      any:
      - resources:
          kinds: [Pod]
          namespaces: [{POLICY_CM_NS}]
          selector:
            matchLabels:
              jupyterhub.io/gitlab-group: {group_name}
    validate:
      message: "{msg}"
      deny:
        conditions:
          any:
          - key: "{jmes}"
            operator: GreaterThan
            value: "{max_gpus}"
"""


def _generate_policy_yaml(groups: list) -> str:
    """Generate the full Kyverno ClusterPolicy YAML from group rules."""
    rules = "".join(
        _build_rule(
            g["gitlab_group"],
            g.get("gpu_type", "nvidia.com/gpu"),
            int(g.get("max_gpus", 0)),
        )
        for g in groups
    )
    return f"""\
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: {KYVERNO_POLICY}
  annotations:
    policies.kyverno.io/title: JupyterHub GPU Group Quotas
    policies.kyverno.io/description: >-
      Enforces per-GitLab-group GPU limits for JupyterHub pods.
      Managed by workbench-admin dashboard — do not edit manually.
spec:
  validationFailureAction: Enforce
  background: false
  rules:
{rules}"""


def _apply_policy(policy_yaml: str) -> tuple:
    """Write the ClusterPolicy to a temp file and apply it via kubectl."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, prefix="kyverno-gpu-"
    )
    try:
        tmp.write(policy_yaml)
        tmp.close()
        # Try local kubectl first, then cp01
        out, rc = _run_local(f"kubectl apply -f {tmp.name} 2>&1")
        if rc != 0 and "command not found" in out:
            # scp the file to cp01, apply there, clean up
            _run_local(f"scp {tmp.name} {CP01_USER}@{CP01_HOST}:{tmp.name}")
            out, rc = _run_on_cp(f"kubectl apply -f {tmp.name} && rm -f {tmp.name}")
        return rc == 0, out
    finally:
        os.unlink(tmp.name)


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.get("/api/gpu-policy")
@require_auth
def get_policy():
    """Return current group rules from the ConfigMap."""
    return jsonify({"groups": _read_policy()})


@bp.post("/api/gpu-policy")
@require_auth
def save_policy():
    """Validate, persist, and apply the GPU group policy."""
    data   = request.get_json(force=True)
    groups = data.get("groups", [])

    # Validate each entry
    for g in groups:
        if not g.get("gitlab_group", "").strip():
            return jsonify({"error": "Every entry must have a gitlab_group."}), 400
        try:
            n = int(g.get("max_gpus", 0))
            if n < 0:
                raise ValueError
            g["max_gpus"] = n
        except (ValueError, TypeError):
            return jsonify({"error": "max_gpus must be a non-negative integer."}), 400
        if not g.get("gpu_type", "").strip():
            g["gpu_type"] = "nvidia.com/gpu"

    # Persist to ConfigMap
    ok, out = _write_policy_cm(groups)
    if not ok:
        return jsonify({"error": f"ConfigMap write failed:\n{out}"}), 500

    # Generate + apply (or delete) Kyverno ClusterPolicy
    if groups:
        policy_yaml = _generate_policy_yaml(groups)
        ok2, out2   = _apply_policy(policy_yaml)
        if not ok2:
            return jsonify({
                "warning": "ConfigMap saved but Kyverno ClusterPolicy apply failed.",
                "detail":  out2,
            }), 207
        return jsonify({
            "success": True,
            "message": f"{len(groups)} rule(s) saved and applied to cluster.",
            "detail":  out2,
        })
    else:
        _kubectl(f"delete clusterpolicy {KYVERNO_POLICY} --ignore-not-found 2>&1")
        return jsonify({
            "success": True,
            "message": "All rules cleared. Kyverno ClusterPolicy removed.",
        })


@bp.get("/api/gpu-policy/status")
@require_auth
def policy_status():
    """Check whether the Kyverno ClusterPolicy exists and is Ready."""
    out, rc = _kubectl(
        f"get clusterpolicy {KYVERNO_POLICY} "
        f"-o jsonpath='{{.status.ready}}' 2>/dev/null"
    )
    if rc != 0 or not out.strip():
        return jsonify({"status": "not_applied"})
    return jsonify({
        "status": "applied",
        "ready":  out.strip().lower() == "true",
    })


@bp.get("/api/gpu-policy/available-groups")
@require_auth
def available_groups():
    """List GitLab groups that have at least one profile defined."""
    return jsonify({"groups": _read_available_groups()})
