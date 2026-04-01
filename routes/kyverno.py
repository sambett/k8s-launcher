"""
routes/kyverno.py — Kyverno policy engine tab.
"""
import json
import os
import subprocess
import tempfile
from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from core.ansible import run_on_cp
from core.paths import COMPAT_MATRIX_PATH, INVENTORY_PATH, VARS_PATH

router = APIRouter()

JHUB_POLICY = """\
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-jhub-resource-limits
  annotations:
    policies.kyverno.io/title: Require JupyterHub Resource Limits
    policies.kyverno.io/description: >
      Ensures all JupyterHub user pods have CPU and memory limits defined.
spec:
  validationFailureAction: Audit
  background: true
  rules:
    - name: check-resource-limits
      match:
        any:
        - resources:
            kinds: [Pod]
            namespaces: [jhub]
      validate:
        message: "JupyterHub pods must have CPU and memory limits defined."
        pattern:
          spec:
            containers:
              - resources:
                  limits:
                    memory: "?*"
                    cpu: "?*"
"""


@router.get("/api/kyverno/status")
async def kyverno_status():
    out, rc = run_on_cp("helm list -n kyverno --no-headers 2>/dev/null | grep kyverno")
    if rc != 0 or not out.strip():
        return {"status": "not_installed"}
    parts = out.strip().split()
    return {"status": "installed", "chart": parts[8] if len(parts) > 8 else "unknown"}


@router.get("/api/kyverno/versions")
async def kyverno_versions():
    if not COMPAT_MATRIX_PATH.exists():
        return {"versions": [], "recommended": None, "k8s_version": ""}
    matrix  = json.loads(COMPAT_MATRIX_PATH.read_text())
    entries = matrix.get("kyverno", [])
    k8s_ver = _read_k8s_version()
    return {"versions": entries, "recommended": _match_version(entries, k8s_ver), "k8s_version": k8s_ver}


@router.get("/api/kyverno/policies")
async def kyverno_policies():
    out, rc = run_on_cp(
        "kubectl get clusterpolicy --no-headers 2>/dev/null "
        "-o custom-columns='NAME:.metadata.name,"
        "READY:.status.ready,"
        "BACKGROUND:.spec.background'"
    )
    policies = []
    if rc == 0 and out.strip():
        for line in out.strip().splitlines():
            parts = line.split()
            if parts:
                policies.append({
                    "name":       parts[0],
                    "ready":      parts[1] if len(parts) > 1 else "unknown",
                    "background": parts[2] if len(parts) > 2 else "unknown",
                })
    return {"policies": policies}


@router.post("/api/kyverno/apply-jhub-policy")
async def apply_jhub_policy():
    tmp = tempfile.mktemp(suffix=".yaml")
    try:
        with open(tmp, "w") as f:
            f.write(JHUB_POLICY)
        result = subprocess.run(
            ["ansible", "control_plane", "-i", str(INVENTORY_PATH),
             "-m", "copy", "-a", f"src={tmp} dest=/tmp/jhub-kyverno-policy.yaml",
             "--extra-vars", f"@{VARS_PATH}"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return {"status": "error", "message": result.stderr}
        out, rc = run_on_cp("kubectl apply -f /tmp/jhub-kyverno-policy.yaml 2>&1")
        if rc != 0:
            return {"status": "error", "message": out}
        return {"status": "ok", "message": "Policy applied in Audit mode"}
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _kyverno_install_stream(version: str):
    if not INVENTORY_PATH.exists():
        yield "data: __ERROR__:no_inventory — run Configure first\n\n"
        return
    yield f"data: Installing Kyverno {version}...\n\n"
    run_on_cp("kubectl create namespace kyverno --dry-run=client -o yaml | kubectl apply -f - 2>&1")
    yield "data: Namespace kyverno ready\n\n"
    out, _ = run_on_cp(
        "helm repo add kyverno https://kyverno.github.io/kyverno/ && helm repo update 2>&1"
    )
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"
    out, rc = run_on_cp(
        f"helm upgrade --install kyverno kyverno/kyverno "
        f"--namespace kyverno --version {version} --timeout 5m --wait 2>&1"
    )
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"
    if rc != 0:
        yield f"data: __ERROR__:{rc}\n\n"
        return
    yield "data: ✓ Kyverno installed. Apply policies from the Policies section.\n\n"
    yield "data: __DONE__\n\n"


@router.get("/api/kyverno/install/stream")
async def kyverno_install_stream(version: str = ""):
    if not version:
        return JSONResponse(status_code=400, content={"error": "version required"})
    return StreamingResponse(
        _kyverno_install_stream(version),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _read_k8s_version() -> str:
    if VARS_PATH.exists():
        for line in VARS_PATH.read_text().splitlines():
            if line.strip().startswith("kubernetes_version:"):
                return line.split(":", 1)[1].strip().strip('"')
    return ""


def _match_version(entries: list, k8s_version: str):
    if not k8s_version or not entries:
        return None
    try:
        minor = int(k8s_version.split(".")[1])
        for entry in entries:
            lo = int(entry.get("k8s_min", "0").split(".")[1])
            hi = int(entry.get("k8s_max", "99").split(".")[1])
            if lo <= minor <= hi:
                return entry["version"]
    except (ValueError, IndexError):
        pass
    return None
