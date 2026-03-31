"""
routes/status.py — kubeconfig download + platform status check.
"""
import json
import os
import subprocess
import tempfile
import urllib.request

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from core.paths import (
    INVENTORY_PATH,
    VARS_PATH,
    GITLAB_OUTPUTS_PATH,
    JUPYTERHUB_VARS_PATH,
    DASHBOARD_VARS_PATH,
    GENERATED_DIR,
)
from core.ansible import run_on_cp, read_cp_ip

router = APIRouter()


# _read_cp_ip moved to core/ansible.py as read_cp_ip()


@router.get("/api/status/kubeconfig")
async def download_kubeconfig():
    if not INVENTORY_PATH.exists():
        raise HTTPException(
            status_code=400,
            detail="No inventory found. Run POST /api/configure first."
        )
    tmp = tempfile.mktemp(suffix=".yaml")
    result = subprocess.run(
        ["ansible", "control_plane",
         "-i", str(INVENTORY_PATH),
         "-m", "fetch",
         "-a", f"src=/etc/kubernetes/admin.conf dest={tmp} flat=yes",
         "--become",
         "--extra-vars", f"@{VARS_PATH}"],
        capture_output=True, text=True
    )
    if result.returncode != 0 or not os.path.exists(tmp):
        raise HTTPException(
            status_code=500,
            detail=f"Could not fetch kubeconfig:\n{result.stderr}"
        )
    return FileResponse(path=tmp, filename="kubeconfig.yaml",
                        media_type="application/x-yaml")


@router.get("/api/platform/status")
async def platform_status():
    """
    Live health check for all platform components.
    Called on page load to auto-restore stepper states.
    """
    result = {}

    # Cluster
    out, rc = run_on_cp("kubectl get nodes --no-headers 2>/dev/null | wc -l")
    lines = [l.strip() for l in out.splitlines() if l.strip().isdigit()]
    node_count = int(lines[0]) if lines else 0
    result["cluster"] = {
        "status":     "ready" if node_count > 0 else "not_deployed",
        "node_count": node_count,
    }

    # Longhorn
    out, rc = run_on_cp(
        "kubectl get pods -n longhorn-system --no-headers 2>/dev/null "
        "| grep Running | wc -l"
    )
    lines = [l.strip() for l in out.splitlines() if l.strip().isdigit()]
    lh_running = int(lines[0]) if lines else 0

    # Get Longhorn UI NodePort dynamically
    lh_url = None
    out_svc, rc_svc = run_on_cp(
        "kubectl get svc longhorn-frontend -n longhorn-system "
        "--no-headers 2>/dev/null | awk '{print $5}'"
    )
    import re as _re
    m = _re.search(r":(\d+)/TCP", out_svc)
    if m and VARS_PATH.exists():
        lh_port = m.group(1)
        cp_ip = read_cp_ip()
        if cp_ip:
            lh_url = f"http://{cp_ip}:{lh_port}"

    result["longhorn"] = {
        "status":      "ready" if lh_running > 0 else "not_deployed",
        "pods_running": lh_running,
        "url":         lh_url,
    }

    # GitLab
    gitlab_url = None
    if GITLAB_OUTPUTS_PATH.exists():
        data = json.loads(GITLAB_OUTPUTS_PATH.read_text())
        gitlab_url = data.get("gitlab_url")
    if gitlab_url:
        try:
            urllib.request.urlopen(f"{gitlab_url}/api/v4/version", timeout=8)
            result["gitlab"] = {"status": "ready", "url": gitlab_url}
        except urllib.error.HTTPError:
            # HTTPError means GitLab responded — it is up
            result["gitlab"] = {"status": "ready", "url": gitlab_url}
        except Exception:
            result["gitlab"] = {"status": "error", "url": gitlab_url}
    else:
        result["gitlab"] = {"status": "not_deployed", "url": None}

    # JupyterHub
    out, rc = run_on_cp(
        "kubectl get pods -n jhub --no-headers 2>/dev/null | grep Running | wc -l"
    )
    lines = [l.strip() for l in out.splitlines() if l.strip().isdigit()]
    jhub_running = int(lines[0]) if lines else 0
    jhub_url = None
    if JUPYTERHUB_VARS_PATH.exists():
        port = 32080
        ip   = ""
        for line in JUPYTERHUB_VARS_PATH.read_text().splitlines():
            if "jhub_access_node_ip" in line:
                ip = line.split(":", 1)[1].strip().strip('"')
            if "jhub_nodeport" in line and "chart" not in line:
                try:
                    port = int(line.split(":", 1)[1].strip())
                except Exception:
                    pass
        if ip:
            jhub_url = f"http://{ip}:{port}"
    result["jupyterhub"] = {
        "status":      "ready" if jhub_running > 0 else "not_deployed",
        "pods_running": jhub_running,
        "url":         jhub_url,
    }

    # Dashboard — compute URL from control plane IP + port in dashboard-vars.yml
    dash_url  = None
    dash_port = 8888
    cp_ip     = read_cp_ip()
    if DASHBOARD_VARS_PATH.exists():
        for line in DASHBOARD_VARS_PATH.read_text().splitlines():
            if line.strip().startswith("dashboard_port:"):
                try:
                    dash_port = int(line.split(":", 1)[1].strip())
                except Exception:
                    pass
        if cp_ip:
            dash_url = f"http://{cp_ip}:{dash_port}"
    result["dashboard"] = {
        "status": "ready" if DASHBOARD_VARS_PATH.exists() else "not_deployed",
        "url":    dash_url,
    }

    return result
