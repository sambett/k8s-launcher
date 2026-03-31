"""
routes/reset.py — Cluster reset and component-level resets.

Cluster reset levels (existing):
  cluster        — remove K8s + Longhorn
  full           — same + SSH keys + inventory
  full_platform  — same + JupyterHub + dashboard + all generated files

Component resets (new — each scoped to one component only):
  GET /api/reset/gitlab/stream      — wipe GitLab state + clear outputs
  GET /api/reset/jupyterhub/stream  — uninstall JupyterHub helm release
  GET /api/reset/dashboard/stream   — remove workbench-admin service
"""
import shutil
import subprocess

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from core.paths import (
    INVENTORY_PATH,
    VARS_PATH,
    GENERATED_DIR,
    GITLAB_INVENTORY_PATH,
    GITLAB_VARS_PATH,
    GITLAB_OUTPUTS_PATH,
    JUPYTERHUB_VARS_PATH,
    DASHBOARD_VARS_PATH,
)

router = APIRouter()

GITLAB_BECOME_PATH = GENERATED_DIR / "gitlab-become.yml"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ansible_cp(cmd: str) -> tuple:
    """Run a shell command on the control plane via ansible."""
    result = subprocess.run(
        ["ansible", "control_plane",
         "-i", str(INVENTORY_PATH),
         "-m", "shell", "-a", cmd,
         "--extra-vars", f"@{VARS_PATH}"],
        capture_output=True, text=True
    )
    return result.stdout + result.stderr, result.returncode


def _ansible_gitlab(cmd: str) -> tuple:
    """Run a shell command on the GitLab VM via ansible."""
    if not GITLAB_INVENTORY_PATH.exists():
        return "no gitlab inventory", 1
    args = ["ansible", "gitlab",
            "-i", str(GITLAB_INVENTORY_PATH),
            "-m", "shell", "-a", cmd,
            "--become",
            "--extra-vars", f"@{GITLAB_VARS_PATH}"]
    if GITLAB_BECOME_PATH.exists():
        args += ["--extra-vars", f"@{GITLAB_BECOME_PATH}"]
    result = subprocess.run(args, capture_output=True, text=True)
    return result.stdout + result.stderr, result.returncode


# ── GitLab component reset ────────────────────────────────────────────────────

# _gitlab_reset_stream lives in core/gitlab_reset.py
from core.gitlab_reset import _gitlab_reset_stream

def _jupyterhub_reset_stream():
    if not INVENTORY_PATH.exists():
        yield "data: __ERROR__:no_inventory — run Configure tab first\n\n"
        return

    yield "data: Starting JupyterHub reset...\n\n"

    yield "data: [jupyterhub] uninstalling Helm release...\n\n"
    out, rc = _ansible_cp(
        "helm uninstall jhub -n jhub 2>/dev/null || true"
    )
    yield "data: [jupyterhub] Helm release removed\n\n"

    yield "data: [jupyterhub] deleting jhub namespace...\n\n"
    out, rc = _ansible_cp(
        "kubectl delete namespace jhub --ignore-not-found 2>/dev/null || true"
    )
    yield "data: [jupyterhub] namespace deleted\n\n"

    yield "data: [jupyterhub] clearing generated vars file...\n\n"
    if JUPYTERHUB_VARS_PATH.exists():
        JUPYTERHUB_VARS_PATH.unlink()
    yield "data: [jupyterhub] vars cleared\n\n"

    yield "data: JupyterHub reset complete — ready for fresh deploy.\n\n"
    yield "data: __DONE__\n\n"


@router.get("/api/reset/jupyterhub/stream")
async def jupyterhub_reset_stream(confirmation: str = ""):
    if confirmation != "RESET":
        return JSONResponse(
            status_code=400,
            content={"error": "Type RESET to confirm."}
        )
    return StreamingResponse(
        _jupyterhub_reset_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Dashboard component reset ─────────────────────────────────────────────────

def _dashboard_reset_stream():
    if not INVENTORY_PATH.exists():
        yield "data: __ERROR__:no_inventory — run Configure tab first\n\n"
        return

    yield "data: Starting Admin Dashboard reset...\n\n"

    yield "data: [dashboard] stopping and disabling service...\n\n"
    out, rc = _ansible_cp(
        "sudo systemctl stop workbench-admin 2>/dev/null || true && "
        "sudo systemctl disable workbench-admin 2>/dev/null || true && "
        "sudo rm -f /etc/systemd/system/workbench-admin.service && "
        "sudo rm -rf /etc/workbench && "
        "sudo systemctl daemon-reload 2>/dev/null || true"
    )
    yield "data: [dashboard] service removed\n\n"

    yield "data: [dashboard] clearing generated vars file...\n\n"
    if DASHBOARD_VARS_PATH.exists():
        DASHBOARD_VARS_PATH.unlink()
    yield "data: [dashboard] vars cleared\n\n"

    yield "data: Admin Dashboard reset complete — ready for fresh deploy.\n\n"
    yield "data: __DONE__\n\n"


@router.get("/api/reset/dashboard/stream")
async def dashboard_reset_stream(confirmation: str = ""):
    if confirmation != "RESET":
        return JSONResponse(
            status_code=400,
            content={"error": "Type RESET to confirm."}
        )
    return StreamingResponse(
        _dashboard_reset_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Cluster-level reset (existing) ────────────────────────────────────────────

def _reset_stream(level: str):
    if not INVENTORY_PATH.exists():
        yield "data: ERROR — no inventory found. Run configure first.\n\n"
        yield "data: __ERROR__:no_inventory\n\n"
        return

    yield "data: Starting reset...\n\n"

    cluster_cmds = [
        ("kubeadm reset",
         "sudo kubeadm reset -f 2>/dev/null || true"),
        ("stop services",
         "sudo systemctl stop kubelet containerd 2>/dev/null || true"),
        ("remove packages",
         "sudo apt-get remove -y --allow-change-held-packages "
         "kubeadm kubelet kubectl containerd containerd.io 2>/dev/null || true"),
        ("autoremove",
         "sudo apt-get autoremove -y 2>/dev/null || true"),
        ("remove k8s dirs",
         "sudo rm -rf /etc/kubernetes /var/lib/etcd /var/lib/kubelet "
         "/var/lib/longhorn /etc/cni /opt/cni /var/lib/containerd"),
        ("remove apt sources",
         "sudo rm -f /etc/apt/sources.list.d/kubernetes.list "
         "/etc/apt/sources.list.d/docker.list "
         "/etc/apt/keyrings/kubernetes-apt-keyring.gpg "
         "/etc/apt/keyrings/docker.asc /etc/apt/keyrings/docker.gpg"),
        ("remove artifacts",
         "sudo rm -rf ~/cluster-artifacts ~/.kube /root/.kube"),
        ("apt update",
         "sudo apt-get update -qq 2>/dev/null || true"),
    ]

    for label, cmd in cluster_cmds:
        yield f"data: [{label}] running on all nodes...\n\n"
        result = subprocess.run(
            ["ansible", "all",
             "-i", str(INVENTORY_PATH),
             "-m", "shell", "-a", cmd,
             "--become",
             "--extra-vars", f"@{VARS_PATH}"],
            capture_output=True, text=True
        )
        status = "done" if result.returncode == 0 else "completed with warnings"
        yield f"data: [{label}] {status}\n\n"

    if level in ("full", "full_platform"):
        yield "data: [full wipe] removing SSH authorized keys...\n\n"
        subprocess.run(
            ["ansible", "all",
             "-i", str(INVENTORY_PATH),
             "-m", "shell", "-a", "rm -f ~/.ssh/authorized_keys",
             "--extra-vars", f"@{VARS_PATH}"],
            capture_output=True, text=True
        )

    if level == "full_platform":
        # Uninstall GitLab CE using nohup shell script (same as component reset)
        if GITLAB_INVENTORY_PATH.exists() and GITLAB_BECOME_PATH.exists():
            import time as _time, os as _os
            script  = "/tmp/platform_gitlab_uninstall.sh"
            logfile = "/tmp/platform_gitlab_uninstall.log"
            inv  = str(GITLAB_INVENTORY_PATH)
            gv   = str(GITLAB_VARS_PATH)
            bv   = str(GITLAB_BECOME_PATH)
            with open(script, "w") as _f:
                _f.write(f"""#!/bin/bash
echo "[$(date)] Starting GitLab CE uninstall..." >> {logfile}
ansible gitlab \\
  -i {inv} \\
  -m shell \\
  -a "sudo gitlab-ctl stop 2>/dev/null || true && sudo apt-get remove -y gitlab-ce 2>/dev/null || true && sudo dpkg --purge gitlab-ce 2>/dev/null || true && sudo rm -rf /etc/gitlab /var/opt/gitlab /var/log/gitlab /opt/gitlab && echo GITLAB_REMOVED" \\
  --become --timeout 600 \\
  --extra-vars @{gv} \\
  --extra-vars @{bv} >> {logfile} 2>&1
echo "SCRIPT_DONE" >> {logfile}
""")
            _os.chmod(script, 0o755)
            if _os.path.exists(logfile):
                _os.remove(logfile)
            subprocess.Popen(
                ["bash", "-c", f"nohup {script} &"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True
            )
            yield "data: [platform] GitLab uninstall running in background...\n\n"
            waited = 0
            while not _os.path.exists(logfile) and waited < 15:
                _time.sleep(1); waited += 1
            done = False; last_pos = 0; idle = 0
            while not done and idle < 700:
                _time.sleep(3); idle += 3
                with open(logfile) as _lf:
                    _lf.seek(last_pos)
                    new_lines = _lf.readlines()
                    last_pos = _lf.tell()
                for line in new_lines:
                    line = line.strip()
                    if not line: continue
                    if "SCRIPT_DONE" in line: done = True; break
                    yield f"data: [platform] {line}\n\n"
                if not new_lines:
                    yield f"data: [platform] waiting for GitLab uninstall... ({idle}s)\n\n"
            with open(logfile) as _lf:
                success = "GITLAB_REMOVED" in _lf.read()
            yield f"data: [platform] GitLab CE {'fully removed' if success else 'uninstall completed'}\n\n"
        else:
            yield "data: [platform] no GitLab inventory — skipping GitLab uninstall\n\n"
        yield "data: [platform] running JupyterHub reset...\n\n"
        for chunk in _jupyterhub_reset_stream():
            yield chunk

        yield "data: [platform] running Dashboard reset...\n\n"
        for chunk in _dashboard_reset_stream():
            yield chunk

        yield "data: [platform] clearing all generated files...\n\n"
        # Explicitly delete key files first before rmtree
        for f in [INVENTORY_PATH, VARS_PATH, GITLAB_OUTPUTS_PATH,
                  GITLAB_VARS_PATH, DASHBOARD_VARS_PATH, JUPYTERHUB_VARS_PATH]:
            if f.exists():
                f.unlink()
        if GENERATED_DIR.exists():
            shutil.rmtree(GENERATED_DIR)
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        (GENERATED_DIR / "group_vars").mkdir(exist_ok=True)
        yield "data: [platform] all generated files cleared\n\n"

    elif level == "full":
        yield "data: [full wipe] clearing generated inventory...\n\n"
        if GENERATED_DIR.exists():
            shutil.rmtree(GENERATED_DIR)
            GENERATED_DIR.mkdir(parents=True, exist_ok=True)
            (GENERATED_DIR / "group_vars").mkdir(exist_ok=True)

    yield "data: Reset complete. Nodes are clean and ready.\n\n"
    yield "data: __DONE__\n\n"


@router.get("/api/reset/stream")
async def reset_stream(level: str = "cluster", confirmation: str = ""):
    if confirmation != "RESET":
        return JSONResponse(
            status_code=400,
            content={"error": "Type RESET in the confirmation field to proceed."}
        )
    if level not in ("cluster", "full", "full_platform"):
        return JSONResponse(
            status_code=400,
            content={"error": "level must be 'cluster', 'full', or 'full_platform'"}
        )
    return StreamingResponse(
        _reset_stream(level),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.get("/api/reset/preview")
async def reset_preview():
    if not INVENTORY_PATH.exists():
        return {"status": "no_inventory",
                "message": "No inventory found. Run Configure first.",
                "nodes": []}

    nodes           = []
    current_section = None

    for line in INVENTORY_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith('[') and line.endswith(']'):
            current_section = line[1:-1]
        elif (line and not line.startswith('#') and not line.startswith('[')
              and current_section in ('control_plane', 'workers')):
            parts    = line.split()
            hostname = parts[0]
            ip       = next(
                (p.split('=')[1] for p in parts[1:] if p.startswith('ansible_host=')),
                ''
            )
            nodes.append({
                "hostname": hostname,
                "ip":       ip,
                "role":     "control-plane" if current_section == "control_plane" else "worker"
            })

    return {"status": "ok", "nodes": nodes}


@router.get("/api/reset/gitlab/stream")
def gitlab_reset_stream(confirmation: str = ""):
    if confirmation != "RESET":
        return JSONResponse(
            status_code=400,
            content={"error": "Type RESET to confirm."}
        )
    return StreamingResponse(
        _gitlab_reset_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
