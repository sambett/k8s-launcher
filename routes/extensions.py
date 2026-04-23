"""
routes/extensions.py — GPU node management and NVIDIA Device Plugin.

Install method: Helm (nvdp/nvidia-device-plugin chart).
GFD (GPU Feature Discovery) is bundled — enable with gfd_enabled=true.
Device plugin version is independent of Kubernetes version (since K8s 1.10).
Real constraint: NVIDIA Container Toolkit >= 1.7.0 on each GPU worker node OS.

Node name resolution:
  _resolve_inventory_hostname() bridges K8s node names to inventory hostnames
  using the node's InternalIP as the common key. Without this, Ansible silently
  fails with "No hosts matched" when the K8s-registered name differs from the
  inventory hostname — every prereq check returns "missing", every fix does nothing.
"""
import json as _json
import re
import subprocess

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.ansible import run_on_cp
from core.paths import BASE_DIR, INVENTORY_PATH, VARS_PATH

router = APIRouter()

VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")


# ── Helpers ────────────────────────────────────────────────────────────────────

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


def _resolve_inventory_hostname(k8s_node: str) -> str:
    """
    Resolve a Kubernetes node name to its inventory.ini hostname via IP lookup.

    Hard-won bug: worker node name vs inventory hostname can differ.
    kubeadm registers the node under whatever hostname the VM reports at join time.
    The inventory records nodes with ansible_host=<IP> and whatever hostname the
    operator typed in Bootstrap/Workers. Any mismatch — case difference, FQDN
    suffix, manual rename — causes Ansible to fail with "No hosts matched" and
    return empty output, making every prereq check report "missing" and every
    fix silently do nothing.

    Resolution path:
      kubectl get node <k8s_node> -> InternalIP
      -> grep ansible_host=<IP> in inventory.ini
      -> return that line's inventory hostname

    Falls back to k8s_node unchanged when:
      - kubectl unreachable (cluster not yet deployed)
      - inventory does not exist (Configure tab not run)
      - IP not found in inventory (node added outside the launcher)
    Callers always receive a usable string; if names already match, nothing changes.
    """
    out, rc = run_on_cp(
        "kubectl get node " + k8s_node
        + " -o jsonpath='{.status.addresses[?(@.type==\"InternalIP\")].address}'"
        + " 2>/dev/null"
    )
    if rc != 0 or not out.strip():
        return k8s_node

    node_ip = _strip(out).strip().strip("'\"")
    if not node_ip:
        return k8s_node

    if not INVENTORY_PATH.exists():
        return k8s_node

    try:
        for line in INVENTORY_PATH.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("[") or stripped.startswith("#"):
                continue
            if f"ansible_host={node_ip}" in stripped:
                parts = stripped.split()
                if parts:
                    return parts[0]
    except Exception:
        pass

    return k8s_node


def _run_on_node(node: str, cmd: str, become: bool = False, timeout: int = 60):
    """
    Run a shell command on a specific cluster node via Ansible ad-hoc.

    node MUST be the inventory hostname — call _resolve_inventory_hostname() first
    when the input comes from kubectl (K8s node name != inventory hostname).
    """
    args = [
        "ansible", node,
        "-i", str(INVENTORY_PATH),
        "-m", "shell",
        "-a", cmd,
        "--extra-vars", f"@{VARS_PATH}",
    ]
    if become:
        args.append("--become")
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        lines = [
            l.strip() for l in (r.stdout + r.stderr).splitlines()
            if l.strip()
            and "| CHANGED |" not in l and "| SUCCESS |" not in l
            and "| rc=" not in l and not l.startswith("WARNING")
        ]
        return (lines[-1] if lines else ""), r.returncode
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s — node may be unresponsive", 1
    except Exception as e:
        return str(e), 1


# ── Models ─────────────────────────────────────────────────────────────────────

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
    ver_out, ver_rc = run_on_cp(
        "helm list -n nvidia-device-plugin --filter '^nvdp$' --no-headers 2>/dev/null"
    )
    ver_clean = _strip(ver_out).strip()
    if ver_rc == 0 and ver_clean:
        version = ""
        m = re.search(r"nvidia-device-plugin-(\d+\.\d+\.\d+)", ver_clean)
        if m:
            version = "v" + m.group(1)
        return {"status": "installed", "version": version or "unknown"}

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
    Recommended version is the entry with "recommended": true.
    Falls back to index 0 if no entry is explicitly marked.
    """
    compat_path = BASE_DIR / "compat_matrix.json"
    try:
        matrix = _json.loads(compat_path.read_text())
    except Exception as e:
        return {"versions": [], "recommended": None, "toolkit_min": "1.7.0", "error": str(e)}

    all_versions = matrix.get("nvidia_device_plugin", [])
    rec_entry    = next((v for v in all_versions if v.get("recommended")), all_versions[0] if all_versions else None)
    recommended  = rec_entry["version"] if rec_entry else None
    toolkit_min  = rec_entry.get("toolkit_min", "1.7.0") if rec_entry else "1.7.0"

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
        # Uses _run_on_node() so Ansible header stripping is consistent.
        yield f"data: [3/4] Configuring NVIDIA runtime on GPU workers...\n\n"
        if INVENTORY_PATH.exists():
            worker_nodes_inv = []
            in_w = False
            for _line in INVENTORY_PATH.read_text().splitlines():
                if "[workers]" in _line:
                    in_w = True; continue
                if _line.startswith("["):
                    in_w = False; continue
                if in_w and _line.strip() and not _line.strip().startswith("#"):
                    worker_nodes_inv.append(_line.split()[0])

            for _node in worker_nodes_inv:
                _ctk_path, _ = _run_on_node(
                    _node,
                    "which nvidia-ctk 2>/dev/null || echo NOT_FOUND"
                )

                if "NOT_FOUND" in _ctk_path or not _ctk_path:
                    yield f"data: {_node}: no nvidia-ctk found — skipping (not a GPU node).\n\n"
                    continue

                yield f"data: {_node}: nvidia-ctk found at {_ctk_path} — configuring containerd...\n\n"
                _out, _rc = _run_on_node(
                    _node,
                    "nvidia-ctk runtime configure --runtime=containerd --set-as-default && systemctl restart containerd",
                    become=True,
                    timeout=120,
                )
                if _rc == 0:
                    yield f"data: {_node}: containerd NVIDIA runtime configured.\n\n"
                else:
                    yield f"data: {_node}: containerd config failed: {_out or 'unknown error'}.\n\n"

        # Step 3b — helm upgrade --install
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

        # Step 4 — bootstrap GPU labels from nvidia-smi so nodes are labeled
        # immediately without waiting for NFD image pull.
        # IMPORTANT: inventory hostnames are used to run nvidia-smi via Ansible,
        # but kubectl label node requires the K8s-registered node name.
        # We resolve the K8s name from the inventory hostname via IP lookup
        # to avoid silently labeling the wrong node.
        yield f"data: Bootstrapping GPU labels from nvidia-smi...\n\n"
        try:
            if not INVENTORY_PATH.exists():
                yield f"data: No inventory found — skipping GPU label bootstrap.\n\n"
            else:
                worker_nodes = []
                in_workers = False
                for line in INVENTORY_PATH.read_text().splitlines():
                    if "[workers]" in line:
                        in_workers = True; continue
                    if line.startswith("["):
                        in_workers = False; continue
                    if in_workers and line.strip() and not line.strip().startswith("#"):
                        worker_nodes.append(line.split()[0])

                # Build IP -> K8s node name map from kubectl once,
                # rather than per-node calls inside the loop.
                k8s_nodes_out, k8s_nodes_rc = run_on_cp(
                    "kubectl get nodes -o json 2>/dev/null"
                )
                k8s_ip_to_name = {}
                if k8s_nodes_rc == 0:
                    try:
                        k8s_data = _json.loads(_strip(k8s_nodes_out))
                        for item in k8s_data.get("items", []):
                            k8s_name = item["metadata"]["name"]
                            for addr in item.get("status", {}).get("addresses", []):
                                if addr.get("type") == "InternalIP":
                                    k8s_ip_to_name[addr["address"]] = k8s_name
                    except Exception:
                        pass

                def _inv_to_k8s_name(inv_hostname):
                    """Resolve inventory hostname -> K8s node name via IP."""
                    for line in INVENTORY_PATH.read_text().splitlines():
                        s = line.strip()
                        if not s or s.startswith("[") or s.startswith("#"):
                            continue
                        parts = s.split()
                        if parts and parts[0] == inv_hostname:
                            for part in parts[1:]:
                                if part.startswith("ansible_host="):
                                    ip = part.split("=", 1)[1]
                                    return k8s_ip_to_name.get(ip, inv_hostname)
                    return inv_hostname

                gpu_labeled = 0
                for node in worker_nodes:
                    gpu_line, rc_smi = _run_on_node(
                        node,
                        "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo NO_GPU"
                    )

                    if "NO_GPU" in gpu_line or not gpu_line or rc_smi != 0:
                        continue

                    try:
                        parts      = [p.strip() for p in gpu_line.split(",")]
                        gpu_name   = parts[0].replace(" ", "-")
                        gpu_memory = parts[1].replace(" ", "")
                    except Exception as ex:
                        yield f"data: {node}: could not parse GPU info ({gpu_line}): {ex}\n\n"
                        continue

                    cuda_ver, _ = _run_on_node(
                        node,
                        "nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: \\([0-9][0-9]*\\.[0-9][0-9]*\\).*/\\1/p' | head -1 || echo unknown"
                    )
                    if not cuda_ver:
                        cuda_ver = "unknown"

                    # Resolve the K8s node name — do NOT use inventory hostname here.
                    # kubectl label node requires the name kubeadm registered, which
                    # may differ from the inventory hostname.
                    k8s_node_name = _inv_to_k8s_name(node)

                    label_cmd = (
                        f"kubectl label node {k8s_node_name} "
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
                        yield f"data: {node} ({k8s_node_name}): {gpu_name} {gpu_memory}{cuda_note} — labeled\n\n"
                        gpu_labeled += 1
                    else:
                        yield f"data: {node}: label failed: {_strip(lbl_out)}\n\n"

                if gpu_labeled > 0:
                    yield f"data: {gpu_labeled} GPU node(s) labeled automatically.\n\n"
                else:
                    yield f"data: No GPUs found on workers — GFD will label nodes once plugin DaemonSet is Running.\n\n"
        except Exception as ex:
            yield f"data: GPU label bootstrap error (non-fatal): {ex}\n\n"

        yield f"data: __DONE__\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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

        yield f"data: [2/3] Deleting nvidia-device-plugin namespace...\n\n"
        ns_out, ns_rc = run_on_cp(
            "kubectl delete namespace nvidia-device-plugin --ignore-not-found 2>&1"
        )
        for line in _strip(ns_out).splitlines():
            if line.strip() and "not found" not in line.lower():
                yield f"data: {line}\n\n"

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

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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


# ── Node prerequisites check ────────────────────────────────────────────────

@router.get("/api/extensions/gpu/node-prereqs")
async def gpu_node_prereqs(node: str):
    """
    Check GPU prerequisites on a specific worker node:
      1. NVIDIA driver     — via nvidia-smi
      2. Container Toolkit — via nvidia-ctk --version (min 1.7.0)
      3. containerd config — grep nvidia-container-runtime in config.toml

    node is the K8s node name — resolved to inventory hostname via IP before
    running Ansible so checks work even when the names differ.
    """
    inv_hostname = _resolve_inventory_hostname(node)
    result = {"node": node, "inv_hostname": inv_hostname}

    # 1. Driver
    drv, _ = _run_on_node(
        inv_hostname,
        "nvidia-smi --query-gpu=driver_version,name --format=csv,noheader 2>/dev/null | head -1 || echo NOT_FOUND"
    )
    if drv and drv != "NOT_FOUND" and "," in drv:
        parts = [p.strip() for p in drv.split(",", 1)]
        result["driver"] = {"status": "ok", "version": parts[0], "gpu_name": parts[1] if len(parts) > 1 else ""}
    elif drv and drv != "NOT_FOUND":
        result["driver"] = {"status": "ok", "version": drv, "gpu_name": ""}
    else:
        result["driver"] = {"status": "missing", "version": "", "gpu_name": ""}

    # 2. Container Toolkit
    ctk, _ = _run_on_node(
        inv_hostname,
        "nvidia-ctk --version 2>/dev/null | head -1 || echo NOT_FOUND"
    )
    if ctk and ctk != "NOT_FOUND":
        m = re.search(r"(\d+\.\d+\.\d+)", ctk)
        ver = m.group(1) if m else ""
        meets = False
        if ver:
            try:
                meets = [int(x) for x in ver.split(".")] >= [1, 7, 0]
            except Exception:
                pass
        result["toolkit"] = {"status": "ok" if meets else "outdated", "version": ver, "meets_minimum": meets}
    else:
        result["toolkit"] = {"status": "missing", "version": "", "meets_minimum": False}

    # 3. containerd NVIDIA runtime
    # nvidia-ctk writes to /etc/containerd/conf.d/99-nvidia.toml — NOT config.toml.
    # We search /etc/containerd/ recursively so the check works regardless of
    # which sub-path nvidia-ctk chose on this OS version.
    ctd, _ = _run_on_node(
        inv_hostname,
        "grep -rl nvidia-container-runtime /etc/containerd/ 2>/dev/null | wc -l"
    )
    try:
        configured = int(ctd.strip()) > 0
    except Exception:
        configured = False

    conf_path, _ = _run_on_node(
        inv_hostname,
        "grep -rl nvidia-container-runtime /etc/containerd/ 2>/dev/null | head -1 || echo not found"
    )

    result["containerd"] = {
        "status":      "ok" if configured else "not_configured",
        "fixable":     result["toolkit"]["status"] == "ok",
        "config_path": conf_path.strip(),
    }
    return result


# ── Fix node — blocking JSON (kept for backward compatibility) ──────────────

@router.post("/api/extensions/gpu/fix-node")
async def fix_gpu_node(body: NodeAction):
    """
    Configure containerd NVIDIA runtime on a specific GPU worker node.
    Resolves K8s node name to inventory hostname before running Ansible.
    Prefer fix_gpu_node_stream for interactive use — this endpoint returns a
    single success/fail with no intermediate output.
    """
    inv_hostname = _resolve_inventory_hostname(body.node_name)
    out, rc = _run_on_node(
        inv_hostname,
        "nvidia-ctk runtime configure --runtime=containerd --set-as-default && systemctl restart containerd",
        become=True,
        timeout=120,
    )
    if rc == 0:
        return {
            "success": True,
            "message": (
                f"containerd NVIDIA runtime configured on {body.node_name}. "
                "Ready to install the device plugin."
            ),
        }

    if not out:
        out = (
            f"Ansible could not reach '{inv_hostname}' — "
            "verify SSH trust (run Bootstrap tab) and check inventory.ini"
        )
    return {"success": False, "message": out}


# ── GET /api/extensions/gpu/fix-node/stream ────────────────────────────────

@router.get("/api/extensions/gpu/fix-node/stream")
async def fix_gpu_node_stream(node: str):
    """
    SSE stream: configure containerd NVIDIA runtime on a GPU worker node.

    Streams live Ansible output so the operator sees exactly what is happening
    rather than a spinner that appears to do nothing.

    node is the K8s node name — resolved to inventory hostname via IP before
    running Ansible. Resolution is reported in the stream for transparency.
    """

    async def _stream():
        if not INVENTORY_PATH.exists():
            yield "data: __ERROR__ No inventory — run Configure tab first\n\n"
            return

        inv_hostname = _resolve_inventory_hostname(node)
        if inv_hostname != node:
            yield f"data: Resolved '{node}' (K8s) → '{inv_hostname}' (inventory)\n\n"
        else:
            yield f"data: Target node: {node}\n\n"

        yield f"data: Configuring containerd NVIDIA runtime...\n\n"

        cmd = [
            "ansible", inv_hostname,
            "-i", str(INVENTORY_PATH),
            "-m", "shell",
            "-a", (
                "nvidia-ctk runtime configure --runtime=containerd --set-as-default"
                " && systemctl restart containerd"
            ),
            "--become",
            "--extra-vars", f"@{VARS_PATH}",
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        for line in iter(proc.stdout.readline, ""):
            s = line.rstrip()
            if s:
                yield f"data: {s}\n\n"

        proc.stdout.close()
        proc.wait()

        if proc.returncode == 0:
            yield f"data: containerd NVIDIA runtime configured on {node} ✓\n\n"
            yield "data: __DONE__\n\n"
        else:
            yield f"data: __ERROR__:{proc.returncode}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
