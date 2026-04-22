"""
routes/extensions.py — GPU node management and NVIDIA Device Plugin.

Install method: Helm (nvdp/nvidia-device-plugin chart).
GFD (GPU Feature Discovery) is bundled — enable with gfd_enabled=true.
Device plugin version is independent of Kubernetes version (since K8s 1.10).
Real constraint: NVIDIA Container Toolkit >= 1.7.0 on each GPU worker node OS.
"""
import json as _json
import re

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
    """Return True if node has the control-plane role."""
    out, rc = run_on_cp(f"kubectl get node {node_name} --no-headers")
    for line in _strip(out).splitlines():
        cols = line.split()
        if len(cols) >= 3 and "control-plane" in cols[2]:
            return True
    return False


class NodeAction(BaseModel):
    node_name: str


class PluginAction(BaseModel):
    version: str


# ── GET /api/extensions/gpu/status ─────────────────────────────────────────
@router.get("/api/extensions/gpu/status")
async def gpu_status():
    """
    Check if the NVIDIA Device Plugin Helm release exists.
    Uses 'helm list' — reliable regardless of namespace contents.
    NFD daemonsets in the same namespace do NOT count as the plugin being installed.
    """
    # Primary: check Helm release named 'nvdp'
    ver_out, ver_rc = run_on_cp(
        "helm list -n nvidia-device-plugin --filter '^nvdp$' --no-headers 2>/dev/null"
    )
    ver_clean = _strip(ver_out).strip()
    if ver_rc == 0 and ver_clean:
        # helm list columns: name  namespace  revision  updated  status  chart  app_version
        # chart column looks like: nvidia-device-plugin-0.17.4
        version = ""
        m = re.search(r"nvidia-device-plugin-(\d+\.\d+\.\d+)", ver_clean)
        if m:
            version = "v" + m.group(1)
        return {"status": "installed", "version": version or "unknown"}

    # Fallback: legacy kubectl-apply install in kube-system
    out2, rc2 = run_on_cp(
        "kubectl get daemonset nvidia-device-plugin-daemonset "
        "-n kube-system --no-headers 2>&1"
    )
    clean2 = _strip(out2)
    if rc2 == 0 and "nvidia-device-plugin" in clean2 and "not found" not in clean2.lower():
        ver_out2, _ = run_on_cp(
            "kubectl get daemonset nvidia-device-plugin-daemonset -n kube-system "
            "-o jsonpath='{.spec.template.spec.containers[0].image}'"
        )
        version = ""
        m = re.search(r":(.+)$", _strip(ver_out2))
        if m:
            version = m.group(1)
        return {"status": "installed", "version": version or "legacy"}

    return {"status": "not installed", "version": ""}


# ── GET /api/extensions/gpu/versions ───────────────────────────────────────
@router.get("/api/extensions/gpu/versions")
async def gpu_versions():
    """
    Return plugin versions from compat_matrix.json.
    Not filtered by K8s version — device plugin is K8s-version independent.
    """
    compat_path = BASE_DIR / "compat_matrix.json"
    try:
        matrix = _json.loads(compat_path.read_text())
    except Exception as e:
        return {"versions": [], "recommended": None, "toolkit_min": "1.7.0", "error": str(e)}

    all_versions = matrix.get("nvidia_device_plugin", [])
    recommended  = all_versions[0]["version"] if all_versions else None
    toolkit_min  = all_versions[0].get("toolkit_min", "1.7.0") if all_versions else "1.7.0"

    return {
        "versions":    all_versions,
        "recommended": recommended,
        "toolkit_min": toolkit_min,
    }


# ── GET /api/extensions/gpu/install/stream ─────────────────────────────────
@router.get("/api/extensions/gpu/install/stream")
async def install_gpu_plugin_stream(version: str, gfd_enabled: bool = True):
    """
    SSE stream: install NVIDIA Device Plugin via Helm.
    gfd_enabled=true enables GPU Feature Discovery — auto-labels nodes with
    GPU model, memory, CUDA info. Required for type-aware scheduling.
    After install, bootstraps GPU labels from nvidia-smi automatically.
    """

    async def _stream():
        try:
            _validate_version(version)
        except ValueError as e:
            yield f"data: __ERROR__ {e}\n\n"
            return

        chart_version = version.lstrip("v")
        gfd_flag      = "true" if gfd_enabled else "false"
        gfd_note      = " + GFD" if gfd_enabled else ""

        # Step 1 — ensure Helm repo exists
        yield f"data: [1/4] Checking NVIDIA Helm repo...\n\n"
        repo_check, _ = run_on_cp(
            "helm repo list 2>/dev/null | grep -q nvdp && echo EXISTS || echo MISSING"
        )
        if "MISSING" in _strip(repo_check):
            yield f"data: [1/4] Adding NVIDIA device plugin Helm repo...\n\n"
            add_out, add_rc = run_on_cp(
                "helm repo add nvdp https://nvidia.github.io/k8s-device-plugin 2>&1"
            )
            for line in _strip(add_out).splitlines():
                if line.strip():
                    yield f"data: {line}\n\n"
            if add_rc != 0:
                yield f"data: __ERROR__ Could not add Helm repo — check CP internet access\n\n"
                return
        else:
            yield f"data: [1/4] Helm repo nvdp already present.\n\n"

        # Step 2 — update repo index
        yield f"data: [2/4] Updating Helm repo...\n\n"
        upd_out, _ = run_on_cp("helm repo update nvdp 2>&1")
        for line in _strip(upd_out).splitlines():
            if line.strip():
                yield f"data: {line}\n\n"

        # Step 3a — configure containerd NVIDIA runtime on all GPU workers
        yield f"data: [3/4] Configuring NVIDIA runtime on GPU workers...\n\n"
        import subprocess as _sub2
        from core.paths import INVENTORY_PATH as _INV2
        if _INV2.exists():
            _worker_nodes2 = []
            _in_w = False
            for _line in _INV2.read_text().splitlines():
                if "[workers]" in _line:
                    _in_w = True; continue
                if _line.startswith("["):
                    _in_w = False; continue
                if _in_w and _line.strip() and not _line.strip().startswith("#"):
                    _worker_nodes2.append(_line.split()[0])
            for _node in _worker_nodes2:
                _cr = _sub2.run(
                    ["ansible", _node, "-i", str(_INV2), "-m", "shell",
                     "-a", "which nvidia-ctk 2>/dev/null || echo NOT_FOUND"],
                    capture_output=True, text=True, timeout=30
                )
                _ctk_check = [
                    l.strip() for l in (_cr.stdout + _cr.stderr).splitlines()
                    if l.strip() and "CHANGED" not in l and "SUCCESS" not in l
                    and "rc=" not in l and not l.startswith("WARNING")
                ]
                _ctk_path = _ctk_check[-1] if _ctk_check else "NOT_FOUND"

                if "NOT_FOUND" in _ctk_path or not _ctk_path:
                    yield f"data: {_node}: no nvidia-ctk found — skipping (not a GPU node).\n\n"
                    continue

                yield f"data: {_node}: nvidia-ctk found at {_ctk_path} — configuring containerd...\n\n"
                _cfg = _sub2.run(
                    ["ansible", _node, "-i", str(_INV2), "-m", "shell",
                     "-a", "nvidia-ctk runtime configure --runtime=containerd --set-as-default && systemctl restart containerd",
                     "--become"],
                    capture_output=True, text=True, timeout=120
                )
                if _cfg.returncode == 0:
                    yield f"data: {_node}: containerd NVIDIA runtime configured.\n\n"
                else:
                    _err = [l.strip() for l in (_cfg.stdout + _cfg.stderr).splitlines()
                            if l.strip() and "CHANGED" not in l and "SUCCESS" not in l and "rc=" not in l]
                    yield f"data: {_node}: containerd config failed: {_err[-1] if _err else 'unknown error'}.\n\n"

        # Step 3b — helm upgrade -i with correct strategy
        yield f"data: [3/4] Installing Device Plugin {version}{gfd_note} via Helm...\n\n"
        cmd = (
            f"helm upgrade -i nvdp nvdp/nvidia-device-plugin "
            f"--version={chart_version} "
            f"--namespace nvidia-device-plugin "
            f"--create-namespace "
            f"--set gfd.enabled={gfd_flag} "
            f"--set migStrategy=none "
            f"--set deviceDiscoveryStrategy=nvml "
            f"2>&1"
        )
        inst_out, inst_rc = run_on_cp(cmd)
        for line in _strip(inst_out).splitlines():
            if line.strip():
                yield f"data: {line}\n\n"

        if inst_rc != 0:
            yield f"data: Helm install failed — see log above.\n\n"
            yield f"data: __ERROR__ helm upgrade failed\n\n"
            return

        yield f"data: [4/4] NVIDIA Device Plugin {version} installed successfully.\n\n"

        if not gfd_enabled:
            yield f"data: __DONE__\n\n"
            return

        # Step 4 — bootstrap GPU labels from nvidia-smi so nodes are
        # labeled immediately without waiting for NFD image pull
        yield f"data: Bootstrapping GPU labels from nvidia-smi...\n\n"
        import subprocess as _sub
        from core.paths import INVENTORY_PATH as _INV
        try:
            if not _INV.exists():
                yield f"data: No inventory found — skipping GPU label bootstrap.\n\n"
            else:
                worker_nodes = []
                in_workers = False
                for line in _INV.read_text().splitlines():
                    if "[workers]" in line:
                        in_workers = True; continue
                    if line.startswith("["):
                        in_workers = False; continue
                    if in_workers and line.strip() and not line.strip().startswith("#"):
                        worker_nodes.append(line.split()[0])

                gpu_labeled = 0
                for node in worker_nodes:
                    r = _sub.run(
                        ["ansible", node, "-i", str(_INV), "-m", "shell",
                         "-a", "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo NO_GPU"],
                        capture_output=True, text=True, timeout=60
                    )
                    raw_lines = [
                        l.strip() for l in (r.stdout + r.stderr).splitlines()
                        if l.strip()
                        and "CHANGED" not in l and "SUCCESS" not in l
                        and "rc=" not in l and not l.startswith("WARNING")
                    ]
                    gpu_line = raw_lines[-1] if raw_lines else "NO_GPU"

                    if "NO_GPU" in gpu_line or not gpu_line or r.returncode != 0:
                        continue

                    try:
                        parts      = [p.strip() for p in gpu_line.split(",")]
                        gpu_name   = parts[0].replace(" ", "-")
                        gpu_memory = parts[1].replace(" ", "")
                    except Exception as ex:
                        yield f"data: {node}: could not parse GPU info ({gpu_line}): {ex}\n\n"
                        continue

                    # Get CUDA version
                    cr = _sub.run(
                        ["ansible", node, "-i", str(_INV), "-m", "shell",
                         "-a", r"nvidia-smi 2>/dev/null | grep -oP 'CUDA Version: \K[0-9.]+' | head -1 || echo unknown"],
                        capture_output=True, text=True, timeout=60
                    )
                    clines = [
                        l.strip() for l in (cr.stdout + cr.stderr).splitlines()
                        if l.strip()
                        and "CHANGED" not in l and "SUCCESS" not in l and "rc=" not in l
                    ]
                    cuda_ver = clines[-1] if clines else "unknown"

                    label_cmd = (
                        f"kubectl label node {node} "
                        f"nvidia.com/gpu.present=true "
                        f"nvidia.com/gpu.product={gpu_name} "
                        f"nvidia.com/gpu.memory={gpu_memory} "
                        f"--overwrite"
                    )
                    if cuda_ver and cuda_ver != "unknown" and "." in cuda_ver:
                        maj, minor = cuda_ver.split(".", 1)
                        label_cmd += (
                            f" nvidia.com/cuda.driver.major={maj}"
                            f" nvidia.com/cuda.driver.minor={minor}"
                        )

                    lbl_out, lbl_rc = run_on_cp(label_cmd)
                    if lbl_rc == 0:
                        cuda_note = f" CUDA {cuda_ver}" if cuda_ver != "unknown" else ""
                        yield f"data: {node}: {gpu_name} {gpu_memory}{cuda_note} — labeled\n\n"
                        gpu_labeled += 1
                    else:
                        yield f"data: {node}: label failed: {_strip(lbl_out)}\n\n"

                if gpu_labeled > 0:
                    yield f"data: {gpu_labeled} GPU node(s) labeled automatically.\n\n"
                else:
                    yield f"data: No GPUs found on workers — GFD will label nodes once NFD master image finishes pulling.\n\n"
        except Exception as ex:
            yield f"data: GPU label bootstrap error (non-fatal): {ex}\n\n"

        yield f"data: __DONE__\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ── GET /api/extensions/uninstall-gpu-plugin/stream ────────────────────────
@router.get("/api/extensions/uninstall-gpu-plugin/stream")
async def uninstall_gpu_plugin_stream(version: str):
    """SSE stream for NVIDIA Device Plugin uninstall."""

    async def _stream():
        try:
            _validate_version(version)
        except ValueError as e:
            yield f"data: __ERROR__ {e}\n\n"
            return

        yield f"data: [1/3] Attempting Helm uninstall...\n\n"
        helm_out, helm_rc = run_on_cp(
            "helm uninstall nvdp -n nvidia-device-plugin 2>&1"
        )

        if helm_rc == 0:
            for line in _strip(helm_out).splitlines():
                if line.strip():
                    yield f"data: {line}\n\n"
            yield f"data: Helm release removed.\n\n"
        else:
            yield f"data: No active Helm release found.\n\n"

        # Always delete the namespace — removes everything regardless of install method
        yield f"data: [2/3] Deleting nvidia-device-plugin namespace...\n\n"
        ns_out, ns_rc = run_on_cp(
            "kubectl delete namespace nvidia-device-plugin --ignore-not-found 2>&1"
        )
        for line in _strip(ns_out).splitlines():
            if line.strip() and "not found" not in line.lower():
                yield f"data: {line}\n\n"

        # Clean up legacy kube-system install
        yield f"data: [3/3] Cleaning up legacy resources...\n\n"
        leg_out, _ = run_on_cp(
            "kubectl delete daemonset nvidia-device-plugin-daemonset "
            "-n kube-system --ignore-not-found 2>&1"
        )
        leg_clean = _strip(leg_out).strip()
        if leg_clean and "not found" not in leg_clean.lower():
            yield f"data: {leg_clean}\n\n"
        else:
            yield f"data: No legacy resources found.\n\n"

        yield f"data: Device Plugin fully uninstalled.\n\n"
        yield f"data: __DONE__\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ── POST /api/extensions/uninstall-gpu-plugin (legacy JSON endpoint) ────────
@router.post("/api/extensions/uninstall-gpu-plugin")
async def uninstall_gpu_plugin(body: PluginAction):
    """Legacy POST endpoint — kept for backward compatibility."""
    try:
        _validate_version(body.version)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    run_on_cp("helm uninstall nvdp -n nvidia-device-plugin 2>&1")
    run_on_cp("kubectl delete namespace nvidia-device-plugin --ignore-not-found 2>&1")
    return {"success": True, "message": "Device Plugin uninstalled"}


# ── GET /api/extensions/gpu/gfd-nodes ──────────────────────────────────────
@router.get("/api/extensions/gpu/gfd-nodes")
async def gfd_nodes():
    """
    Return worker nodes with GFD-detected GPU labels and allocatable GPU count.
    Single kubectl call — no per-node queries.
    """
    out, rc = run_on_cp("kubectl get nodes -o json 2>/dev/null")
    if rc != 0:
        return {"nodes": [], "error": "kubectl get nodes failed"}

    clean = _strip(out).strip()
    try:
        data = _json.loads(clean)
    except Exception as e:
        return {"nodes": [], "error": f"JSON parse failed: {e}"}

    nodes = []
    for item in data.get("items", []):
        meta   = item.get("metadata", {})
        labels = meta.get("labels", {})
        status = item.get("status", {})

        if "node-role.kubernetes.io/control-plane" in labels:
            continue

        name       = meta.get("name", "")
        node_ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in status.get("conditions", [])
        )

        gpu_product = labels.get("nvidia.com/gpu.product", "")
        gpu_memory  = labels.get("nvidia.com/gpu.memory", "")
        cuda_major  = labels.get("nvidia.com/cuda.driver.major", "")
        cuda_minor  = labels.get("nvidia.com/cuda.driver.minor", "")
        gpu_present = labels.get("nvidia.com/gpu.present", "")
        accelerator = labels.get("accelerator", "")
        allocatable = status.get("allocatable", {}).get("nvidia.com/gpu", "0") or "0"

        cuda_ceiling = ""
        if cuda_major and cuda_minor:
            cuda_ceiling = f"{cuda_major}.{cuda_minor}"
        elif cuda_major:
            cuda_ceiling = cuda_major

        nodes.append({
            "name":         name,
            "status":       "Ready" if node_ready else "NotReady",
            "gfd_active":   bool(gpu_present or gpu_product),
            "gpu_product":  gpu_product,
            "gpu_memory":   gpu_memory,
            "cuda_ceiling": cuda_ceiling,
            "allocatable":  allocatable,
            "legacy_label": accelerator == "nvidia",
        })

    return {"nodes": nodes}


# ── GET /api/extensions/nodes ──────────────────────────────────────────────
@router.get("/api/extensions/nodes")
async def list_nodes():
    """Return all nodes with role field."""
    out, rc = run_on_cp("kubectl get nodes --no-headers")
    if rc != 0:
        return {"nodes": [], "error": out}
    nodes = []
    for line in _strip(out).splitlines():
        cols = line.split()
        if len(cols) < 3:
            continue
        name  = cols[0]
        roles = cols[2]
        nodes.append({
            "name": name,
            "role": "control-plane" if "control-plane" in roles else "worker",
        })
    return {"nodes": nodes}
