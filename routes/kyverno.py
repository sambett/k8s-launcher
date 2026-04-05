"""
routes/kyverno.py — Kyverno policy engine: version selection, install, status.

Follows the same pattern as routes/extensions.py (GPU Device Plugin):
  GET /api/kyverno/versions          — read compat_matrix, filter by k8s version
  GET /api/kyverno/install/stream    — SSE: install via Ansible, version as query param
  GET /api/kyverno/status            — pod-level health check
  GET /api/kyverno/policies          — list active ClusterPolicies (read-only)

Policy CRUD lives in workbench-admin (Day-2). This file is Day-0 only.
"""
import json
import re

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from core.ansible import ansible_stream, run_on_cp
from core.paths import BASE_DIR, ANSIBLE_KYVERNO_DIR

router = APIRouter()

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")   # Helm chart versions: X.Y.Z (no v prefix)


def _get_k8s_minor() -> str:
    """Return current cluster minor version as '1.30' string."""
    out, _ = run_on_cp(
        "kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}'"
    )
    m = re.search(r"v?(\d+\.\d+)", out)
    return m.group(1) if m else ""


# ── Versions ───────────────────────────────────────────────────────────────────

@router.get("/api/kyverno/versions")
async def kyverno_versions():
    """
    Return Kyverno chart versions compatible with the current cluster.
    Reads compat_matrix.json → filters by k8s_min/k8s_max → marks recommended.
    Same pattern as GET /api/extensions/gpu/versions.
    """
    try:
        matrix = json.loads((BASE_DIR / "compat_matrix.json").read_text())
    except Exception as e:
        return {"versions": [], "k8s_version": "", "recommended": None, "error": str(e)}

    k8s_version = _get_k8s_minor()          # e.g. "1.30"
    all_versions = matrix.get("kyverno", [])

    compatible = []
    recommended = None

    for v in all_versions:
        if k8s_version:
            try:
                kv = tuple(int(x) for x in k8s_version.split("."))
                mn = tuple(int(x) for x in v.get("k8s_min", "0.0").split("."))
                mx = tuple(int(x) for x in v.get("k8s_max", "99.99").split("."))
                if kv < mn or kv > mx:
                    continue
            except Exception:
                pass
        compatible.append(v)
        if recommended is None:
            recommended = v["version"]   # first match is recommended

    return {
        "versions":    compatible,
        "k8s_version": k8s_version,
        "recommended": recommended,
    }


# ── Install ────────────────────────────────────────────────────────────────────

@router.get("/api/kyverno/install/stream")
async def kyverno_install_stream(version: str):
    """
    SSE endpoint: install Kyverno via ansible-kyverno playbook.
    version is passed as a query param (e.g. ?version=3.2.6).
    Ansible receives it as --extra-vars "kyverno_version=3.2.6".
    Emits __DONE__ on success, __ERROR__:<rc> on failure.
    """
    async def _stream():
        if not VERSION_RE.match(version):
            yield f"data: __ERROR__ Invalid version '{version}' — expected X.Y.Z (e.g. 3.2.6)\n\n"
            return
        yield f"data: Installing Kyverno {version}...\n\n"
        for chunk in ansible_stream(
            ANSIBLE_KYVERNO_DIR,
            extra_vars={"kyverno_version": version}
        ):
            yield chunk

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Status ─────────────────────────────────────────────────────────────────────

@router.get("/api/kyverno/status")
async def kyverno_status():
    """
    Return running state of all Kyverno pods.
    Used by the launcher dashboard to show an install status badge.
    """
    out, rc = run_on_cp("kubectl get pods -n kyverno --no-headers 2>&1")

    if rc != 0 or not out.strip() or "No resources found" in out:
        return {"installed": False, "running": False, "pods": []}

    pods = []
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            pods.append({"name": parts[0], "ready": parts[1], "status": parts[2]})

    all_running = all(p["status"] == "Running" for p in pods) if pods else False
    return {"installed": len(pods) > 0, "running": all_running, "pods": pods}


# ── Policies ───────────────────────────────────────────────────────────────────

@router.get("/api/kyverno/policies")
async def list_policies():
    """
    List ClusterPolicies active in the cluster (read-only).
    Policies are created/managed by workbench-admin GPU Policies tab.
    """
    out, rc = run_on_cp("kubectl get clusterpolicies --no-headers 2>&1")

    if rc != 0 or not out.strip() or "No resources found" in out:
        return {"policies": []}

    policies = []
    for line in out.strip().splitlines():
        parts = line.split()
        if parts:
            policies.append({
                "name":  parts[0],
                "ready": parts[1] if len(parts) > 1 else "unknown",
                "age":   parts[-1] if len(parts) > 2 else "unknown",
            })
    return {"policies": policies}
