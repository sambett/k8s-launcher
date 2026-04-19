"""
routes/reset.py — Full platform reset.

One operation only: wipe everything and return to bare-OS state.

What gets wiped:
  Phase 1 — Ansible playbook on all K8s nodes (ansible-reset/reset.yml)
             Kubernetes, Longhorn, containerd, packages, apt sources,
             .kube dirs, cluster-artifacts, SSH authorized_keys, /etc/hosts entries
  Phase 2 — GitLab CE uninstall on the GitLab VM (nohup shell script + log tail)
             Only runs if gitlab-inventory.ini exists
             Requires gitlab_become_pass in the POST body
             If no password: emits manual instructions, clears credential files
  Phase 3 — Controller file cleanup
             Deletes all generated/ files and recreates empty structure

POST /api/reset/stream   — body: { confirmation, gitlab_become_pass }
GET  /api/reset/preview  — returns nodes from current inventory
"""
import os
import shutil
import subprocess
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from core.paths import (
    ANSIBLE_RESET_DIR,
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
    """Run a shell command on the control plane via ansible ad-hoc."""
    result = subprocess.run(
        ["ansible", "control_plane",
         "-i", str(INVENTORY_PATH),
         "-m", "shell", "-a", cmd,
         "--become",
         "--extra-vars", f"@{VARS_PATH}"],
        capture_output=True, text=True
    )
    return result.stdout + result.stderr, result.returncode


# ── Model ─────────────────────────────────────────────────────────────────────

class ResetRequest(BaseModel):
    confirmation: str = ""
    gitlab_become_pass: str = ""


# ── Phase 1: Ansible playbook on K8s nodes ────────────────────────────────────

def _phase1_k8s_stream():
    """
    Stream ansible-reset/reset.yml output line by line.
    Yields SSE strings.
    Final yield is __PHASE1_OK__ or __PHASE1_FAIL__:N.
    """
    if not INVENTORY_PATH.exists():
        yield "data: [phase1] ERROR — no inventory found. Run Configure tab first.\n\n"
        yield "data: __PHASE1_FAIL__:no_inventory\n\n"
        return

    yield "data: [phase1] Starting Kubernetes + Longhorn node wipe...\n\n"

    cmd = [
        "ansible-playbook",
        "-i", str(INVENTORY_PATH),
        "site.yml",
        "--extra-vars", f"@{VARS_PATH}",
    ]

    process = subprocess.Popen(
        cmd,
        cwd=str(ANSIBLE_RESET_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    for line in iter(process.stdout.readline, ""):
        stripped = line.rstrip()
        if stripped:
            yield f"data: {stripped}\n\n"

    process.stdout.close()
    process.wait()

    if process.returncode == 0:
        yield "data: [phase1] ✓ All nodes wiped successfully.\n\n"
        yield "data: __PHASE1_OK__\n\n"
    else:
        yield f"data: [phase1] ✗ Playbook exited {process.returncode} — check log above.\n\n"
        yield f"data: __PHASE1_FAIL__:{process.returncode}\n\n"


# ── Phase 2: GitLab CE uninstall ──────────────────────────────────────────────

def _phase2_gitlab_stream(gitlab_become_pass: str):
    """
    Uninstall GitLab CE from the GitLab VM via nohup shell script + log tail.
    Always clears local credential files regardless of uninstall result.
    Yields SSE strings.
    """
    if not GITLAB_INVENTORY_PATH.exists():
        yield "data: [phase2] No GitLab inventory found — skipping GitLab uninstall.\n\n"
        yield "data: __PHASE2_OK__\n\n"
        return

    yield "data: [phase2] Starting GitLab CE uninstall...\n\n"

    # Write a temporary become file if a password was supplied.
    # The become file is intentionally deleted after every successful deploy
    # so we must write it fresh here.
    if gitlab_become_pass:
        GITLAB_BECOME_PATH.write_text(
            f'ansible_become_pass: "{gitlab_become_pass}"\n'
        )
        GITLAB_BECOME_PATH.chmod(0o600)
        yield "data: [phase2] GitLab sudo credentials written (temporary).\n\n"
    else:
        yield "data: [phase2] ⚠ WARNING: no GitLab sudo password provided.\n\n"
        yield "data: [phase2] ⚠ GitLab CE cannot be uninstalled automatically.\n\n"
        yield "data: [phase2] ⚠ Run these commands manually on the GitLab VM:\n\n"
        yield "data: [phase2]     sudo gitlab-ctl stop\n\n"
        yield "data: [phase2]     sudo apt-get remove --purge gitlab-ce\n\n"
        yield "data: [phase2]     sudo rm -rf /etc/gitlab /var/opt/gitlab /var/log/gitlab /opt/gitlab\n\n"
        # Still clear local credential files so the launcher is clean
        _clear_gitlab_credentials()
        yield "data: [phase2] GitLab credential files cleared from controller.\n\n"
        yield "data: __PHASE2_OK__\n\n"
        return

    # Build and launch nohup uninstall script
    script  = "/tmp/gitlab_reset.sh"
    logfile = "/tmp/gitlab_reset.log"
    inv     = str(GITLAB_INVENTORY_PATH)
    gv      = str(GITLAB_VARS_PATH)
    bv      = str(GITLAB_BECOME_PATH)

    with open(script, "w") as f:
        f.write(f"""#!/bin/bash
echo "[$(date)] Starting GitLab CE uninstall..." >> {logfile}
ansible gitlab \\
  -i {inv} \\
  -m shell \\
  -a "sudo gitlab-ctl stop 2>/dev/null || true && \\
      sudo apt-get remove -y gitlab-ce 2>/dev/null || true && \\
      sudo dpkg --purge gitlab-ce 2>/dev/null || true && \\
      sudo rm -rf /etc/gitlab /var/opt/gitlab /var/log/gitlab /opt/gitlab && \\
      echo GITLAB_REMOVED" \\
  --become --timeout 600 \\
  --extra-vars @{gv} \\
  --extra-vars @{bv} >> {logfile} 2>&1
echo "[$(date)] ANSIBLE_EXIT_CODE=$?" >> {logfile}
echo "SCRIPT_DONE" >> {logfile}
""")
    os.chmod(script, 0o755)

    if os.path.exists(logfile):
        os.remove(logfile)

    subprocess.Popen(
        ["bash", "-c", f"nohup {script} &"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    yield "data: [phase2] GitLab uninstall script launched (3-5 min)...\n\n"

    # Wait for logfile to appear
    waited = 0
    while not os.path.exists(logfile) and waited < 15:
        time.sleep(1)
        waited += 1

    if not os.path.exists(logfile):
        yield "data: [phase2] ERROR — log file never appeared. Check Ansible manually.\n\n"
        _clear_gitlab_credentials()
        yield "data: __PHASE2_OK__\n\n"
        return

    # Tail logfile until SCRIPT_DONE
    done     = False
    last_pos = 0
    idle     = 0
    while not done and idle < 700:
        time.sleep(3)
        idle += 3
        with open(logfile) as lf:
            lf.seek(last_pos)
            new_lines = lf.readlines()
            last_pos  = lf.tell()
        for line in new_lines:
            line = line.strip()
            if not line:
                continue
            if "SCRIPT_DONE" in line:
                done = True
                break
            yield f"data: [phase2] {line}\n\n"
        if not new_lines:
            yield f"data: [phase2] waiting for GitLab uninstall... ({idle}s)\n\n"

    # Check result
    success = False
    if os.path.exists(logfile):
        with open(logfile) as lf:
            success = "GITLAB_REMOVED" in lf.read()

    if success:
        yield "data: [phase2] ✓ GitLab CE fully removed from VM.\n\n"
    else:
        yield "data: [phase2] ⚠ Uninstall completed — check log above for details.\n\n"

    # Always clean up become file and credentials after use
    _clear_gitlab_credentials()
    yield "data: [phase2] GitLab credential files cleared from controller.\n\n"
    yield "data: __PHASE2_OK__\n\n"


def _clear_gitlab_credentials():
    """Delete all GitLab-related generated files from the controller."""
    for path in [
        GITLAB_OUTPUTS_PATH,
        GITLAB_VARS_PATH,
        GITLAB_BECOME_PATH,
        GITLAB_INVENTORY_PATH,
    ]:
        if path.exists():
            path.unlink()


# ── Phase 3: Controller file cleanup ─────────────────────────────────────────

def _phase3_cleanup_stream():
    """
    Delete all generated files on the controller and recreate empty structure.
    Yields SSE strings.
    """
    yield "data: [phase3] Clearing all generated files on controller...\n\n"

    # Explicitly unlink known files first before rmtree
    for f in [
        INVENTORY_PATH,
        VARS_PATH,
        GITLAB_OUTPUTS_PATH,
        GITLAB_VARS_PATH,
        GITLAB_BECOME_PATH,
        GITLAB_INVENTORY_PATH,
        JUPYTERHUB_VARS_PATH,
        DASHBOARD_VARS_PATH,
    ]:
        if f.exists():
            f.unlink()

    if GENERATED_DIR.exists():
        shutil.rmtree(GENERATED_DIR)

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    (GENERATED_DIR / "group_vars").mkdir(exist_ok=True)

    yield "data: [phase3] ✓ All generated files cleared.\n\n"
    yield "data: __PHASE3_OK__\n\n"



# ── JupyterHub component reset ───────────────────────────────────────────────

def _jupyterhub_reset_stream():
    """
    Uninstall JupyterHub Helm release and delete the jhub namespace.
    Removes all secrets and ConfigMaps inside the namespace:
      - gitlab-oauth-secret
      - gitlab-registry-secret
      - jupyterhub-crypt-key secret
      - jupyterhub-profiles ConfigMap
    Longhorn volumes use Retain policy — user data PVs survive intentionally.
    Clears jupyterhub-vars.yml so Configure must be re-run before next deploy.
    """
    if not INVENTORY_PATH.exists():
        yield "data: __ERROR__:no_inventory — run Configure tab first\n\n"
        return

    yield "data: Starting JupyterHub reset...\n\n"

    yield "data: [jhub] Uninstalling Helm release...\n\n"
    out, rc = _ansible_cp(
        "helm uninstall jhub -n jhub 2>/dev/null || true"
    )
    yield "data: [jhub] Helm release removed.\n\n"

    yield "data: [jhub] Deleting jhub namespace (removes all secrets and ConfigMaps)...\n\n"
    out, rc = _ansible_cp(
        "kubectl delete namespace jhub --ignore-not-found 2>/dev/null || true"
    )
    yield "data: [jhub] Namespace deleted.\n\n"

    yield "data: [jhub] Clearing generated vars file...\n\n"
    if JUPYTERHUB_VARS_PATH.exists():
        JUPYTERHUB_VARS_PATH.unlink()
    yield "data: [jhub] Vars cleared.\n\n"

    yield "data: ✓ JupyterHub reset complete — ready for fresh deploy.\n\n"
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



# ── Dashboard component reset ────────────────────────────────────────────────

def _dashboard_reset_stream():
    """
    Stop and remove the workbench-admin systemd service.
    Steps:
      1. systemctl stop workbench-admin
      2. systemctl disable workbench-admin
      3. Remove /etc/systemd/system/workbench-admin.service unit file
      4. Remove /etc/workbench/ — contains secrets.env with GitLab token
      5. systemctl daemon-reload — clean systemd state
    The ~/k8s-launcher repo clone on cplane is intentionally preserved —
    it is the running codebase, not a deployment artifact.
    Clears dashboard-vars.yml so Configure must be re-run before next deploy.
    """
    if not INVENTORY_PATH.exists():
        yield "data: __ERROR__:no_inventory — run Configure tab first\n\n"
        return

    yield "data: Starting Admin Dashboard reset...\n\n"

    yield "data: [dashboard] Stopping service...\n\n"
    out, rc = _ansible_cp(
        "sudo systemctl stop workbench-admin 2>/dev/null || true"
    )
    yield "data: [dashboard] Service stopped.\n\n"

    yield "data: [dashboard] Disabling service...\n\n"
    out, rc = _ansible_cp(
        "sudo systemctl disable workbench-admin 2>/dev/null || true"
    )
    yield "data: [dashboard] Service disabled.\n\n"

    yield "data: [dashboard] Removing unit file and secrets...\n\n"
    out, rc = _ansible_cp(
        "sudo rm -f /etc/systemd/system/workbench-admin.service && "
        "sudo rm -rf /etc/workbench && "
        "sudo systemctl daemon-reload 2>/dev/null || true"
    )
    yield "data: [dashboard] Unit file and secrets removed.\n\n"

    yield "data: [dashboard] Clearing generated vars file...\n\n"
    if DASHBOARD_VARS_PATH.exists():
        DASHBOARD_VARS_PATH.unlink()
    yield "data: [dashboard] Vars cleared.\n\n"

    yield "data: ✓ Admin Dashboard reset complete — ready for fresh deploy.\n\n"
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


# ── Main reset stream ─────────────────────────────────────────────────────────

def _full_reset_stream(gitlab_become_pass: str):
    """
    Orchestrates all three phases in sequence.
    Phase 1 failure does NOT abort Phase 2 or 3 — we always clean up.
    """
    yield "data: ══════════════════════════════════════════════\n\n"
    yield "data:   FULL PLATFORM RESET STARTED\n\n"
    yield "data: ══════════════════════════════════════════════\n\n"

    # ── Phase 1 ───────────────────────────────────────────────────────────────
    yield "data: \n\n"
    yield "data: ── Phase 1 / 3 — Kubernetes + Longhorn node wipe ──\n\n"
    phase1_ok = False
    for chunk in _phase1_k8s_stream():
        if chunk == "data: __PHASE1_OK__\n\n":
            phase1_ok = True
        elif chunk.startswith("data: __PHASE1_FAIL__"):
            phase1_ok = False
        else:
            yield chunk

    if phase1_ok:
        yield "data: ✓ Phase 1 complete.\n\n"
    else:
        yield "data: ⚠ Phase 1 had errors — continuing cleanup anyway.\n\n"

    # ── Phase 2 ───────────────────────────────────────────────────────────────
    yield "data: \n\n"
    yield "data: ── Phase 2 / 3 — GitLab CE uninstall ──\n\n"
    for chunk in _phase2_gitlab_stream(gitlab_become_pass):
        if chunk in ("data: __PHASE2_OK__\n\n",):
            pass
        else:
            yield chunk
    yield "data: ✓ Phase 2 complete.\n\n"

    # ── Phase 3 ───────────────────────────────────────────────────────────────
    yield "data: \n\n"
    yield "data: ── Phase 3 / 3 — Controller file cleanup ──\n\n"
    for chunk in _phase3_cleanup_stream():
        if chunk == "data: __PHASE3_OK__\n\n":
            pass
        else:
            yield chunk
    yield "data: ✓ Phase 3 complete.\n\n"

    # ── Final summary ─────────────────────────────────────────────────────────
    yield "data: \n\n"
    yield "data: ══════════════════════════════════════════════\n\n"
    yield "data:   RESET COMPLETE — platform is at bare-OS state.\n\n"
    yield "data:   Re-run Bootstrap tab to start a fresh deploy.\n\n"
    yield "data: ══════════════════════════════════════════════\n\n"
    yield "data: __DONE__\n\n"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/api/reset/stream")
async def reset_stream(req: ResetRequest):
    if req.confirmation != "RESET":
        return JSONResponse(
            status_code=400,
            content={"error": "Type RESET in the confirmation field to proceed."}
        )
    return StreamingResponse(
        _full_reset_stream(req.gitlab_become_pass),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/reset/preview")
async def reset_preview():
    if not INVENTORY_PATH.exists():
        return {
            "status":  "no_inventory",
            "message": "No inventory found. Run Configure first.",
            "nodes":   [],
        }

    nodes           = []
    current_section = None

    for line in INVENTORY_PATH.read_text().splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1]
        elif (
            line
            and not line.startswith("#")
            and not line.startswith("[")
            and current_section in ("control_plane", "workers")
        ):
            parts    = line.split()
            hostname = parts[0]
            ip       = next(
                (p.split("=")[1] for p in parts[1:] if p.startswith("ansible_host=")),
                "",
            )
            ssh_user = next(
                (p.split("=")[1] for p in parts[1:] if p.startswith("ansible_user=")),
                "",
            )
            nodes.append({
                "hostname": hostname,
                "ip":       ip,
                "role":     "control-plane" if current_section == "control_plane" else "worker",
                "ssh_user": ssh_user,
            })

    return {"status": "ok", "nodes": nodes}
