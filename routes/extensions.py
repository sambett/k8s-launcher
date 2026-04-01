"""
routes/extensions.py — GPU tab (NVIDIA device plugin + node labeling).
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


@router.get("/api/extensions/gpu/status")
async def gpu_status():
    out, rc = run_on_cp(
        "kubectl get daemonset nvidia-device-plugin-daemonset "
        "-n kube-system --no-headers 2>/dev/null"
    )
    if rc != 0 or not out.strip():
        return {"status": "not_installed"}
    out_img, _ = run_on_cp(
        "kubectl get daemonset nvidia-device-plugin-daemonset -n kube-system "
        "-o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null"
    )
    version = out_img.split(":")[-1].strip("'") if ":" in out_img else "unknown"
    return {"status": "installed", "version": version}


@router.get("/api/extensions/gpu/versions")
async def gpu_versions():
    if not COMPAT_MATRIX_PATH.exists():
        return {"versions": [], "recommended": None, "k8s_version": ""}
    matrix = json.loads(COMPAT_MATRIX_PATH.read_text())
    entries = matrix.get("nvidia_device_plugin", [])
    k8s_ver = _read_k8s_version()
    return {"versions": entries, "recommended": _match_version(entries, k8s_ver), "k8s_version": k8s_ver}


@router.get("/api/extensions/gpu/nodes")
async def gpu_nodes():
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


@router.post("/api/extensions/gpu/label-node")
async def label_gpu_node(req: GPULabelRequest):
    out, rc = run_on_cp(
        f"kubectl label node {req.node_name} accelerator=nvidia --overwrite 2>&1"
    )
    if rc != 0:
        return {"status": "error", "message": out}
    return {"status": "ok", "message": f"Node {req.node_name} labeled accelerator=nvidia"}


@router.post("/api/extensions/gpu/unlabel-node")
async def unlabel_gpu_node(req: GPULabelRequest):
    out, rc = run_on_cp(
        f"kubectl label node {req.node_name} accelerator- 2>&1 || true"
    )
    if rc != 0:
        return {"status": "error", "message": out}
    return {"status": "ok", "message": f"Label removed from {req.node_name}"}


def _gpu_install_stream(version: str):
    if not INVENTORY_PATH.exists():
        yield "data: __ERROR__:no_inventory — run Configure first\n\n"
        return
    url = (
        f"https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/"
        f"{version}/deployments/static/nvidia-device-plugin.yml"
    )
    yield f"data: Installing NVIDIA Device Plugin {version}...\n\n"
    out, rc = run_on_cp(f"kubectl apply -f {url} 2>&1")
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"
    if rc != 0:
        yield f"data: __ERROR__:{rc}\n\n"
        return
    yield "data: Waiting for DaemonSet rollout...\n\n"
    out, _ = run_on_cp(
        "kubectl rollout status daemonset/nvidia-device-plugin-daemonset "
        "-n kube-system --timeout=60s 2>&1"
    )
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"
    yield f"data: ✓ NVIDIA Device Plugin {version} installed.\n\n"
    yield "data: __DONE__\n\n"


@router.get("/api/extensions/gpu/install/stream")
async def gpu_install_stream(version: str = ""):
    if not version:
        return JSONResponse(status_code=400, content={"error": "version required"})
    return StreamingResponse(
        _gpu_install_stream(version),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/extensions/nodes")
async def list_nodes():
    out, rc = run_on_cp(
        "kubectl get nodes --no-headers "
        "-o custom-columns='NAME:.metadata.name,"
        "ROLE:.metadata.labels.node-role\\.kubernetes\\.io/worker' 2>/dev/null"
    )
    nodes = []
    if rc == 0 and out.strip():
        for line in out.strip().splitlines():
            parts = line.split()
            if parts:
                role = "worker" if len(parts) > 1 and parts[1] != "<none>" else "control-plane"
                nodes.append({"name": parts[0], "role": role})
    return {"nodes": nodes}


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
