"""
routes/extensions.py — GPU tab: NVIDIA device plugin install + node labeling.

Design notes
------------
- Version strings are validated with a regex before reaching any URL or kubectl
  call.  This is the root cause fix for the v0.17. trailing-dot 404 bug.
- The plugin manifest is downloaded to a temp file on the control plane first,
  then applied locally.  This separates the network-fetch step from the
  kubectl-apply step, giving clearer errors when GitHub is unreachable vs when
  the manifest itself is invalid.
- Node labels are applied/removed via kubectl label — this is a live cluster
  operation, not a provisioning step, so Ansible is not involved.
"""

import json
import re
from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from core.ansible import run_on_cp
from core.paths import COMPAT_MATRIX_PATH, INVENTORY_PATH, VARS_PATH

router = APIRouter()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Valid version tag: vMAJOR.MINOR.PATCH — no trailing dot, no spaces
_VERSION_RE = re.compile(r'^v\d+\.\d+\.\d+$')

_MANIFEST_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin"
    "/{version}/deployments/static/nvidia-device-plugin.yml"
)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class GPULabelRequest(BaseModel):
    node_name: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_version(version: str) -> str | None:
    """Return None if version is valid, or an error message string if not."""
    if not version or not version.strip():
        return "Version is required."
    if not _VERSION_RE.match(version.strip()):
        return (
            f"Invalid version format: '{version}'. "
            "Expected vMAJOR.MINOR.PATCH (e.g. v0.17.4)."
        )
    return None


def _read_k8s_version() -> str:
    if VARS_PATH.exists():
        for line in VARS_PATH.read_text().splitlines():
            if line.strip().startswith("kubernetes_version:"):
                return line.split(":", 1)[1].strip().strip('"')
    return ""


def _match_version(entries: list, k8s_version: str) -> str | None:
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


# ---------------------------------------------------------------------------
# Install stream
# ---------------------------------------------------------------------------

def _gpu_install_stream(version: str):
    """
    SSE generator for the install log.

    Steps:
      1. Guard checks (inventory exists, version is valid).
      2. curl download manifest → /tmp/nvidia-device-plugin-<version>.yml on CP.
      3. kubectl apply -f /tmp/...
      4. kubectl rollout status (60 s timeout).
      5. rm temp file.
    """
    if not INVENTORY_PATH.exists():
        yield "data: ERROR — inventory not found. Run Configure first.\n\n"
        yield "data: __ERROR__:no_inventory\n\n"
        return

    version = version.strip()
    err = _validate_version(version)
    if err:
        yield f"data: ERROR — {err}\n\n"
        yield "data: __ERROR__:bad_version\n\n"
        return

    manifest_url = _MANIFEST_URL_TEMPLATE.format(version=version)
    tmp_path     = f"/tmp/nvidia-device-plugin-{version}.yml"

    yield f"data: Installing NVIDIA Device Plugin {version}...\n\n"
    yield  "data: Step 1/3 — Downloading manifest from GitHub...\n\n"

    dl_out, dl_rc = run_on_cp(
        f"curl -fsSL --retry 3 --retry-delay 2 "
        f"-o {tmp_path} '{manifest_url}' 2>&1 && echo '__DL_OK__'"
    )
    for line in dl_out.splitlines():
        if line.strip():
            yield f"data:   {line}\n\n"

    if dl_rc != 0 or "__DL_OK__" not in dl_out:
        yield f"data: ERROR — could not download manifest from:\n\n"
        yield f"data:   {manifest_url}\n\n"
        yield f"data: Check that tag {version} exists in the NVIDIA GitHub repo.\n\n"
        yield  "data: __ERROR__:download_failed\n\n"
        return

    yield f"data: Manifest saved to {tmp_path}\n\n"
    yield  "data: Step 2/3 — Applying manifest...\n\n"

    apply_out, apply_rc = run_on_cp(f"kubectl apply -f {tmp_path} 2>&1")
    for line in apply_out.splitlines():
        if line.strip():
            yield f"data:   {line}\n\n"

    if apply_rc != 0:
        yield "data: ERROR — kubectl apply failed. See log above.\n\n"
        yield f"data: __ERROR__:{apply_rc}\n\n"
        run_on_cp(f"rm -f {tmp_path} 2>/dev/null || true")
        return

    yield "data: Step 3/3 — Waiting for DaemonSet rollout (60 s timeout)...\n\n"
    rollout_out, _ = run_on_cp(
        "kubectl rollout status daemonset/nvidia-device-plugin-daemonset "
        "-n kube-system --timeout=60s 2>&1"
    )
    for line in rollout_out.splitlines():
        if line.strip():
            yield f"data:   {line}\n\n"

    run_on_cp(f"rm -f {tmp_path} 2>/dev/null || true")

    yield f"data: ✓ NVIDIA Device Plugin {version} installed successfully.\n\n"
    yield  "data: __DONE__\n\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/api/extensions/gpu/status")
async def gpu_status():
    """Return install status and current version of the device plugin DaemonSet."""
    out, rc = run_on_cp(
        "kubectl get daemonset nvidia-device-plugin-daemonset "
        "-n kube-system --no-headers 2>/dev/null"
    )
    if rc != 0 or not out.strip():
        return {"status": "not_installed"}

    img_out, _ = run_on_cp(
        "kubectl get daemonset nvidia-device-plugin-daemonset -n kube-system "
        "-o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null"
    )
    version = img_out.split(":")[-1].strip("'") if ":" in img_out else "unknown"
    return {"status": "installed", "version": version}


@router.get("/api/extensions/gpu/versions")
async def gpu_versions():
    """Return version list from compat_matrix.json with the recommended version highlighted."""
    if not COMPAT_MATRIX_PATH.exists():
        return {"versions": [], "recommended": None, "k8s_version": ""}

    matrix  = json.loads(COMPAT_MATRIX_PATH.read_text())
    entries = matrix.get("nvidia_device_plugin", [])
    k8s_ver = _read_k8s_version()
    return {
        "versions":    entries,
        "recommended": _match_version(entries, k8s_ver),
        "k8s_version": k8s_ver,
    }


@router.get("/api/extensions/gpu/install/stream")
async def gpu_install_stream(version: str = ""):
    """SSE endpoint — streams install progress for the given plugin version."""
    err = _validate_version(version)
    if err:
        return JSONResponse(status_code=400, content={"error": err})

    return StreamingResponse(
        _gpu_install_stream(version.strip()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/extensions/nodes")
async def list_nodes():
    """List all cluster nodes with role labels (used to populate the label dropdown)."""
    out, rc = run_on_cp(
        "kubectl get nodes --no-headers "
        "-o custom-columns='"
        "NAME:.metadata.name,"
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


@router.get("/api/extensions/gpu/nodes")
async def gpu_nodes():
    """List nodes currently carrying the accelerator=nvidia label."""
    out, rc = run_on_cp(
        "kubectl get nodes -l accelerator=nvidia --no-headers "
        "-o custom-columns='"
        "NAME:.metadata.name,"
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
    """Apply the accelerator=nvidia label to the specified node."""
    if not req.node_name or not req.node_name.strip():
        return JSONResponse(status_code=400, content={"error": "node_name is required"})

    out, rc = run_on_cp(
        f"kubectl label node {req.node_name} accelerator=nvidia --overwrite 2>&1"
    )
    if rc != 0:
        return {"status": "error", "message": out}
    return {"status": "ok", "message": f"Node '{req.node_name}' labeled accelerator=nvidia."}


@router.post("/api/extensions/gpu/unlabel-node")
async def unlabel_gpu_node(req: GPULabelRequest):
    """Remove the accelerator=nvidia label from the specified node."""
    if not req.node_name or not req.node_name.strip():
        return JSONResponse(status_code=400, content={"error": "node_name is required"})

    out, rc = run_on_cp(
        f"kubectl label node {req.node_name} accelerator- 2>&1"
    )
    # rc=1 with "not found" means label was already absent — treat as success
    if rc != 0 and "not found" not in out.lower():
        return {"status": "error", "message": out}
    return {"status": "ok", "message": f"Label removed from node '{req.node_name}'."}
