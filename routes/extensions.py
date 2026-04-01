"""
routes/extensions.py — Optional cluster extensions (GPU, monitoring, ingress).

Current extensions:
  GPU — NVIDIA device plugin DaemonSet + node labeling

All extension installs are idempotent (kubectl apply).
Version suggestions come from compat_matrix.json nvidia_device_plugin section.
"""
import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from core.ansible import run_on_cp
from core.paths import COMPAT_MATRIX_PATH, INVENTORY_PATH, VARS_PATH

router = APIRouter()


class GPULabelRequest(BaseModel):
    node_name: str


# ── GPU status ────────────────────────────────────────────────────────────────

@router.get("/api/extensions/gpu/status")
async def gpu_status():
    """Check whether the NVIDIA device plugin DaemonSet is installed."""
    out, rc = run_on_cp(
        "kubectl get daemonset nvidia-device-plugin-daemonset "
        "-n kube-system --no-headers 2>/dev/null"
    )
    if rc != 0 or not out.strip():
        return {"status": "not_installed"}

    # Read image tag for version
    out_img, _ = run_on_cp(
        "kubectl get daemonset nvidia-device-plugin-daemonset -n kube-system "
        "-o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null"
    )
    version = out_img.split(":")[-1].strip("'") if ":" in out_img else "unknown"
    return {"status": "installed", "version": version}


# ── GPU version suggestions from compat matrix ────────────────────────────────

@router.get("/api/extensions/gpu/versions")
async def gpu_versions():
    """
    Return NVIDIA device plugin versions from compat_matrix.json.
    Marks the recommended version for the current cluster K8s version.
    """
    if not COMPAT_MATRIX_PATH.exists():
        return {"versions": [], "recommended": None, "k8s_version": ""}

    matrix = json.loads(COMPAT_MATRIX_PATH.read_text())
    entries = matrix.get("nvidia_device_plugin", [])

    # Read current K8s version from generated vars
    k8s_version = ""
    if VARS_PATH.exists():
        for line in VARS_PATH.read_text().splitlines():
            if line.strip().startswith("kubernetes_version:"):
                k8s_version = line.split(":", 1)[1].strip().strip('"')
                break

    recommended = None
    if k8s_version and entries:
        try:
            minor = int(k8s_version.split(".")[1])
            for entry in entries:
                k8s_min = int(entry.get("k8s_min", "0").split(".")[1])
                k8s_max = int(entry.get("k8s_max", "99").split(".")[1])
                if k8s_min <= minor <= k8s_max:
                    recommended = entry["version"]
                    break
        except (ValueError, IndexError):
            pass

    return {
        "versions":    entries,
        "recommended": recommended,
        "k8s_version": k8s_version,
    }


# ── GPU nodes ─────────────────────────────────────────────────────────────────

@router.get("/api/extensions/gpu/nodes")
async def gpu_nodes():
    """List nodes labeled accelerator=nvidia with their GPU resource count."""
    out, rc = run_on_cp(
        "kubectl get nodes -l accelerator=nvidia --no-headers "
        "-o custom-columns="
        "'NAME:.metadata.name,"
        "STATUS:.status.conditions[-1].type,"
        "GPU:.status.allocatable.nvidia\\.io/gpu' 2>/dev/null"
    )
    nodes = []
    if rc == 0 and out.strip():
        for line in out.strip().splitlines():
            parts = line.split()
            if parts:
                nodes.append({
                    "name":      parts[0],
                    "status":    parts[1] if len(parts) > 1 else "unknown",
                    "gpu_count": parts[2] if len(parts) > 2 else "<none>",
                })
    return {"nodes": nodes}


# ── GPU node labeling ─────────────────────────────────────────────────────────

@router.post("/api/extensions/gpu/label-node")
async def label_gpu_node(req: GPULabelRequest):
    """Label a node as GPU-capable so the device plugin targets it."""
    out, rc = run_on_cp(
        f"kubectl label node {req.node_name} accelerator=nvidia --overwrite"
    )
    if rc != 0:
        return {"status": "error", "message": out}
    return {
        "status":  "ok",
        "message": f"Node {req.node_name} labeled accelerator=nvidia",
    }


# ── GPU install stream ────────────────────────────────────────────────────────

def _gpu_install_stream(version: str):
    if not INVENTORY_PATH.exists():
        yield "data: __ERROR__:no_inventory — run Configure first\n\n"
        return

    yield f"data: Installing NVIDIA Device Plugin {version}...\n\n"

    manifest_url = (
        f"https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/"
        f"{version}/deployments/static/nvidia-device-plugin.yml"
    )
    yield f"data: Manifest: {manifest_url}\n\n"
    yield "data: Applying to cluster...\n\n"

    out, rc = run_on_cp(f"kubectl apply -f {manifest_url}")
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"

    if rc != 0:
        yield f"data: __ERROR__:{rc}\n\n"
        return

    yield "data: Waiting for DaemonSet rollout (up to 60s)...\n\n"
    out, rc = run_on_cp(
        "kubectl rollout status daemonset/nvidia-device-plugin-daemonset "
        "-n kube-system --timeout=60s 2>/dev/null || echo 'rollout-pending'"
    )
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"

    yield f"data: NVIDIA Device Plugin {version} installed.\n\n"
    yield "data: Next: label GPU nodes using the form below.\n\n"
    yield "data: Nodes with NVIDIA drivers installed will automatically\n\n"
    yield "data: advertise nvidia.com/gpu as a schedulable resource.\n\n"
    yield "data: __DONE__\n\n"


@router.get("/api/extensions/gpu/install/stream")
async def gpu_install_stream(version: str = ""):
    if not version:
        return JSONResponse(
            status_code=400,
            content={"error": "version parameter required"}
        )
    return StreamingResponse(
        _gpu_install_stream(version),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Cluster nodes list (for dropdowns) ───────────────────────────────────────

@router.get("/api/extensions/nodes")
async def list_nodes():
    """Return all cluster nodes — used by label dropdown."""
    out, rc = run_on_cp(
        "kubectl get nodes --no-headers "
        "-o custom-columns='NAME:.metadata.name,ROLE:.metadata.labels.node-role\\.kubernetes\\.io/worker' "
        "2>/dev/null"
    )
    nodes = []
    if rc == 0 and out.strip():
        for line in out.strip().splitlines():
            parts = line.split()
            if parts and parts[0] != "<none>":
                role = "worker" if len(parts) > 1 and parts[1] != "<none>" else "control-plane"
                nodes.append({"name": parts[0], "role": role})
    return {"nodes": nodes}
