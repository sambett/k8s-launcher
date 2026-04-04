"""
routes/extensions.py — GPU node management and NVIDIA Device Plugin.

Uses run_on_cp() for all kubectl calls — ansiblectl has no local kubeconfig.
kubectl runs on ansiblecplane (the test cluster control plane) via SSH.
"""

import os
import re
import subprocess
import tempfile

from fastapi import APIRouter
from pydantic import BaseModel

from core.ansible import run_on_cp

router = APIRouter()

VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def _strip(out):
    """Remove Ansible ad-hoc metadata lines before parsing kubectl output.

    run_on_cp() prepends:  'hostname | CHANGED | rc=0 >>'
    That line must be removed before any line-by-line kubectl parsing.
    """
    clean = []
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Skip ansible metadata lines
        if "| CHANGED |" in stripped or "| SUCCESS |" in stripped or "| rc=" in stripped:
            continue
        # Skip kubectl column header line
        if stripped.startswith("NAME") and "STATUS" in stripped:
            continue
        clean.append(line)
    return "\n".join(clean)


def _validate_version(version):
    if not VERSION_RE.match(version):
        raise ValueError(
            f"Invalid version {version!r}. Expected vX.Y.Z (e.g. v0.17.4)"
        )


def _is_control_plane(node_name):
    out, _ = run_on_cp(
        f"kubectl get node {node_name} -o "
        f"jsonpath=\'{{.metadata.labels.node-role\\.kubernetes\\.io/control-plane}}\'"
    )
    return bool(out.strip())


class NodeAction(BaseModel):
    node: str


class PluginAction(BaseModel):
    version: str


@router.get("/api/extensions/nodes")
async def list_nodes():
    """Return worker nodes only — control-plane always excluded."""
    out, rc = run_on_cp(
        "kubectl get nodes -o "
        "jsonpath=\'{range .items[*]}{.metadata.name}{\"\\t\"}"
        "{.metadata.labels.node-role\\.kubernetes\\.io/control-plane}{\"\\t\"}"
        "{.metadata.labels.accelerator}{\"\\n\"}{end}\'"
    )
    if rc != 0:
        return {"error": out}
    nodes = []
    for line in _strip(out).splitlines():
        parts = line.split("\t")
        if not parts[0]:
            continue
        cp_label = parts[1] if len(parts) > 1 else ""
        if cp_label:
            continue
        gpu_lbl = parts[2].strip() if len(parts) > 2 else ""
        nodes.append({
            "name":      parts[0],
            "is_gpu":    gpu_lbl == "nvidia",
            "gpu_label": gpu_lbl or ""
        })
    return nodes


@router.get("/api/extensions/gpu-nodes")
async def gpu_nodes():
    """Return all worker nodes with GPU label status."""
    out, rc = run_on_cp("kubectl get nodes --show-labels")
    if rc != 0:
        return {"error": out}
    rows = []
    for line in _strip(out).splitlines():
        cols = line.split()
        if len(cols) < 5:
            continue
        labels = {}
        for kv in cols[-1].split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                labels[k] = v
        if "node-role.kubernetes.io/control-plane" in labels:
            continue
        rows.append({
            "name":      cols[0],
            "status":    cols[1],
            "gpu_label": labels.get("accelerator", ""),
            "is_gpu":    labels.get("accelerator") == "nvidia"
        })
    return rows


@router.post("/api/extensions/label-gpu")
async def label_gpu_node(body: NodeAction):
    node = body.node.strip()
    if not node:
        return {"success": False, "message": "node name is required"}
    if _is_control_plane(node):
        return {"success": False, "message": f"Refused: {node!r} is the control-plane node"}
    out, rc = run_on_cp(f"kubectl label node {node} accelerator=nvidia --overwrite")
    if rc == 0:
        return {"success": True, "message": f"Node {node!r} labeled as GPU node"}
    return {"success": False, "message": out}


@router.post("/api/extensions/unlabel-gpu")
async def unlabel_gpu_node(body: NodeAction):
    node = body.node.strip()
    if not node:
        return {"success": False, "message": "node name is required"}
    if _is_control_plane(node):
        return {"success": False, "message": f"Refused: {node!r} is the control-plane node"}
    out, rc = run_on_cp(f"kubectl label node {node} accelerator-")
    if rc == 0:
        return {"success": True, "message": f"GPU label removed from {node!r}"}
    if "not found" in out.lower() or "not labeled" in out.lower():
        return {"success": True, "message": f"Node {node!r} had no GPU label"}
    return {"success": False, "message": out}


@router.post("/api/extensions/install-gpu-plugin")
async def install_gpu_plugin(body: PluginAction):
    try:
        _validate_version(body.version)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    version = body.version
    url = (f"https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/"
           f"{version}/deployments/static/nvidia-device-plugin.yml")
    tmp = f"/tmp/nvidia-device-plugin-{version}.yml"
    # Download on ansiblectl, then copy to CP and apply
    dl = subprocess.run(["curl", "-fsSL", "-o", tmp, url], capture_output=True, text=True)
    if dl.returncode != 0:
        return {"success": False,
                "message": f"Download failed for {version}",
                "detail": dl.stderr}
    # scp the manifest to the CP then kubectl apply
    scp_out, scp_rc = run_on_cp(f"test -d /tmp && echo ok")
    if scp_rc != 0:
        return {"success": False, "message": "Cannot reach control plane"}
    # Copy file via run_on_cp cat trick
    with open(tmp) as f:
        manifest = f.read()
    remote_tmp = f"/tmp/nvidia-device-plugin-{version}.yml"
    # Write to CP using heredoc via run_on_cp
    write_out, write_rc = run_on_cp(
        f"cat > {remote_tmp} << \'MANIFEST_EOF\'\n{manifest}\nMANIFEST_EOF"
    )
    ap_out, ap_rc = run_on_cp(f"kubectl apply -f {remote_tmp}")
    if ap_rc == 0:
        return {"success": True,
                "message": f"NVIDIA Device Plugin {version} installed",
                "detail": ap_out}
    return {"success": False, "message": "kubectl apply failed", "detail": ap_out}


@router.post("/api/extensions/uninstall-gpu-plugin")
async def uninstall_gpu_plugin(body: PluginAction):
    try:
        _validate_version(body.version)
    except ValueError as e:
        return {"success": False, "message": str(e)}
    version = body.version
    remote_tmp = f"/tmp/nvidia-device-plugin-{version}.yml"
    # Check if manifest exists on CP
    check_out, check_rc = run_on_cp(f"test -f {remote_tmp} && echo exists || echo missing")
    if "missing" in check_out:
        # Re-download to CP directly
        url = (f"https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/"
               f"{version}/deployments/static/nvidia-device-plugin.yml")
        dl_out, dl_rc = run_on_cp(f"curl -fsSL -o {remote_tmp} {url}")
        if dl_rc != 0:
            return {"success": False, "message": "Could not download manifest on CP", "detail": dl_out}
    out, rc = run_on_cp(f"kubectl delete -f {remote_tmp} --ignore-not-found")
    if rc == 0:
        return {"success": True, "message": f"NVIDIA Device Plugin {version} removed"}
    return {"success": False, "message": out}
