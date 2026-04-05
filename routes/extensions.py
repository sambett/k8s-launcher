"""
routes/extensions.py — GPU node management and NVIDIA Device Plugin.

All API paths match the frontend in templates/tabs/extensions.html.
run_on_cp() is used for all kubectl calls — ansiblectl has no local kubeconfig.
"""
import json
import re
import subprocess

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.ansible import run_on_cp
from core.paths import BASE_DIR

router = APIRouter()

VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def _strip(out):
    """Remove Ansible ad-hoc metadata lines before parsing kubectl output."""
    clean = []
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        if "| CHANGED |" in s or "| SUCCESS |" in s or "| rc=" in s:
            continue
        if s.startswith("NAME") and "STATUS" in s:
            continue
        clean.append(line)
    return "\n".join(clean)


def _validate_version(version):
    if not VERSION_RE.match(version):
        raise ValueError(
            f"Invalid version {version!r}. Expected vX.Y.Z (e.g. v0.17.4)"
        )


def _is_control_plane(node_name):
    """Return True if node has the control-plane role.
    Uses --no-headers + ROLES column — avoids all jsonpath escaping issues.
    """
    out, rc = run_on_cp(f"kubectl get node {node_name} --no-headers")
    for line in _strip(out).splitlines():
        cols = line.split()
        if len(cols) >= 3 and "control-plane" in cols[2]:
            return True
    return False


def _get_k8s_minor():
    """Return the server k8s minor version as a '1.30' style string."""
    out, _ = run_on_cp(
        "kubectl get nodes -o jsonpath=\'{.items[0].status.nodeInfo.kubeletVersion}\'"
    )
    # out looks like: v1.30.5
    m = re.search(r"v?(\d+\.\d+)", out)
    return m.group(1) if m else ""


class NodeAction(BaseModel):
    node_name: str   # frontend sends { node_name: "worker01" }


class PluginAction(BaseModel):
    version: str


# ── GET /api/extensions/gpu/status ─────────────────────────────────────────
@router.get("/api/extensions/gpu/status")
async def gpu_status():
    """Check if the NVIDIA Device Plugin DaemonSet is installed."""
    out, rc = run_on_cp(
        "kubectl get daemonset nvidia-device-plugin-daemonset "
        "-n kube-system --no-headers 2>&1"
    )
    clean = _strip(out)
    if rc == 0 and "nvidia-device-plugin" in clean:
        # Try to extract the image tag as a proxy for version
        ver_out, _ = run_on_cp(
            "kubectl get daemonset nvidia-device-plugin-daemonset -n kube-system "
            "-o jsonpath=\'{.spec.template.spec.containers[0].image}\'"
        )
        version = ""
        m = re.search(r":(.+)$", _strip(ver_out))
        if m:
            version = m.group(1)
        return {"status": "installed", "version": version}
    return {"status": "not installed", "version": ""}


# ── GET /api/extensions/gpu/versions ───────────────────────────────────────
@router.get("/api/extensions/gpu/versions")
async def gpu_versions():
    """Return compatible plugin versions from compat_matrix.json."""
    compat_path = BASE_DIR / "compat_matrix.json"
    try:
        matrix = json.loads(compat_path.read_text())
    except Exception as e:
        return {"versions": [], "k8s_version": "", "recommended": None, "error": str(e)}

    k8s_version = _get_k8s_minor()          # e.g. "1.30"
    all_versions = matrix.get("nvidia_device_plugin", [])

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
            recommended = v["version"]

    return {
        "versions":    compatible,
        "k8s_version": k8s_version,
        "recommended": recommended,
    }


# ── GET /api/extensions/gpu/install/stream ─────────────────────────────────
@router.get("/api/extensions/gpu/install/stream")
async def install_gpu_plugin_stream(version: str):
    """SSE stream for NVIDIA Device Plugin installation."""

    async def _stream():
        # Step 1 — validate version format
        try:
            _validate_version(version)
        except ValueError as e:
            yield f"data: __ERROR__ {e}\n\n"

            return

        url = (
            f"https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/"
            f"{version}/deployments/static/nvidia-device-plugin.yml"
        )
        remote_tmp = f"/tmp/nvidia-device-plugin-{version}.yml"

        # Step 2 — download manifest directly on the control plane
        # No file copy needed — avoids all heredoc / escaping corruption
        yield f"data: [1/3] Downloading manifest {version} on control plane...\n\n"
        dl_out, dl_rc = run_on_cp(f"curl -fsSL -o {remote_tmp} '{url}'")
        if dl_rc != 0:
            err = _strip(dl_out).strip()[:300]
            yield f"data: Download failed: {err}\n\n"
            yield f"data: __ERROR__ Download failed — check CP internet access\n\n"
            return
        yield f"data: [1/3] Manifest saved to {remote_tmp}\n\n"
        # Step 3 — verify downloaded file is non-empty
        verify_out, verify_rc = run_on_cp(
            f"test -s {remote_tmp} && head -3 {remote_tmp} || echo FILE_EMPTY"
        )
        if "FILE_EMPTY" in _strip(verify_out) or verify_rc != 0:
            yield f"data: __ERROR__ Downloaded file is empty — bad URL or no connectivity\n\n"
            return
        yield f"data: [2/3] File verified. Applying with kubectl...\n\n"
        # Step 4 — kubectl apply
        ap_out, ap_rc = run_on_cp(f"kubectl apply -f {remote_tmp}")
        for line in _strip(ap_out).splitlines():
            if line.strip():
                yield f"data: {line}\n\n"
        if ap_rc == 0:
            yield f"data: [3/3] NVIDIA Device Plugin {version} installed successfully.\n\n"
            yield f"data: __DONE__\n\n"
        else:
            yield f"data: kubectl apply failed.\n\n"
            yield f"data: __ERROR__ apply failed\n\n"
    return StreamingResponse(_stream(), media_type="text/event-stream")


# ── GET /api/extensions/nodes ──────────────────────────────────────────────
@router.get("/api/extensions/nodes")
async def list_nodes():
    """Return all nodes with role field. Frontend filters by role == 'worker'."""
    # --no-headers avoids jsonpath escaping issues; ROLES column tells us the role
    out, rc = run_on_cp("kubectl get nodes --no-headers")
    if rc != 0:
        return {"nodes": [], "error": out}
    nodes = []
    for line in _strip(out).splitlines():
        cols = line.split()
        if len(cols) < 3:
            continue
        name  = cols[0]
        roles = cols[2]   # "control-plane" or "<none>"
        # Fetch accelerator label per node
        lbl_out, _ = run_on_cp(
            f"kubectl get node {name} -o jsonpath='{{.metadata.labels.accelerator}}'"
        )
        gpu_label = _strip(lbl_out).strip()
        nodes.append({
            "name":      name,
            "role":      "control-plane" if "control-plane" in roles else "worker",
            "is_gpu":    gpu_label == "nvidia",
            "gpu_label": gpu_label,
        })
    return {"nodes": nodes}

@router.post("/api/extensions/gpu/label-node")
async def label_gpu_node(body: NodeAction):
    """Label a worker node with accelerator=nvidia."""
    node = body.node_name.strip()
    if not node:
        return {"status": "error", "message": "node name is required"}
    if _is_control_plane(node):
        return {"status": "error",
                "message": f"Refused: {node!r} is the control-plane — cannot label it as GPU"}
    out, rc = run_on_cp(f"kubectl label node {node} accelerator=nvidia --overwrite")
    if rc == 0:
        return {"status": "ok", "message": f"Node \'{node}\' labeled as GPU node (accelerator=nvidia)"}
    return {"status": "error", "message": _strip(out)}


# ── POST /api/extensions/gpu/unlabel-node ──────────────────────────────────
@router.post("/api/extensions/gpu/unlabel-node")
async def unlabel_gpu_node(body: NodeAction):
    """Remove accelerator=nvidia label from a node."""
    node = body.node_name.strip()
    if not node:
        return {"status": "error", "message": "node name is required"}
    if _is_control_plane(node):
        return {"status": "error",
                "message": f"Refused: {node!r} is the control-plane"}
    out, rc = run_on_cp(f"kubectl label node {node} accelerator-")
    if rc == 0:
        return {"status": "ok", "message": f"GPU label removed from \'{node}\'"}
    if "not found" in out.lower() or "not labeled" in out.lower():
        return {"status": "ok", "message": f"Node \'{node}\' had no GPU label"}
    return {"status": "error", "message": _strip(out)}


# ── GET /api/extensions/gpu/nodes ──────────────────────────────────────────
@router.get("/api/extensions/gpu/nodes")
async def gpu_nodes():
    """Return worker nodes that carry the accelerator=nvidia label."""
    out, rc = run_on_cp("kubectl get nodes --show-labels")
    if rc != 0:
        return {"nodes": [], "error": out}
    nodes = []
    for line in _strip(out).splitlines():
        cols = line.split()
        if len(cols) < 5:
            continue
        labels = {}
        for kv in cols[-1].split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                labels[k] = v
        # Skip control plane
        if "node-role.kubernetes.io/control-plane" in labels:
            continue
        # Only nodes labeled as GPU
        if labels.get("accelerator") != "nvidia":
            continue
        # Get allocatable GPU count (0 if driver not installed)
        gpu_out, _ = run_on_cp(
            f"kubectl get node {cols[0]} -o "
            f"jsonpath=\'{{{{{{\.status\.allocatable[\'nvidia\.com/gpu\']}}}}}}\'  2>/dev/null || echo 0"
        )
        nodes.append({
            "name":      cols[0],
            "status":    cols[1],
            "gpu_count": _strip(gpu_out).strip() if _strip(gpu_out).strip().isdigit() else "0",
        })
    return {"nodes": nodes}


# ── POST /api/extensions/uninstall-gpu-plugin (kept for completeness) ──────
@router.post("/api/extensions/uninstall-gpu-plugin")
async def uninstall_gpu_plugin(body: PluginAction):
    try:
        _validate_version(body.version)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    version    = body.version
    remote_tmp = f"/tmp/nvidia-device-plugin-{version}.yml"
    check_out, _ = run_on_cp(f"test -f {remote_tmp} && echo exists || echo missing")
    if "missing" in check_out:
        url = (
            f"https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/"
            f"{version}/deployments/static/nvidia-device-plugin.yml"
        )
        dl_out, dl_rc = run_on_cp(f"curl -fsSL -o {remote_tmp} {url}")
        if dl_rc != 0:
            return {"success": False,
                    "message": "Could not re-download manifest on CP",
                    "detail": dl_out}
    out, rc = run_on_cp(f"kubectl delete -f {remote_tmp} --ignore-not-found")
    if rc == 0:
        return {"success": True, "message": f"NVIDIA Device Plugin {version} removed"}
    return {"success": False, "message": _strip(out)}
