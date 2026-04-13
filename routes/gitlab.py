"""
routes/gitlab.py — GitLab tab
"""
import json
import socket
import subprocess

import paramiko
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.paths import (
    ANSIBLE_GITLAB_DIR,
    ANSIBLE_K8S_DIR,
    GITLAB_INVENTORY_PATH,
    GITLAB_VARS_PATH,
    GITLAB_OUTPUTS_PATH,
    INVENTORY_PATH,
    GENERATED_DIR,
    SSH_KEY_PATH,
    SSH_PUB_KEY_PATH,
)
from core.ssh import get_client_with_password, run_command

router = APIRouter()

GITLAB_BECOME_PATH = GENERATED_DIR / "gitlab-become.yml"


class GitLabSSHRequest(BaseModel):
    ip: str
    ssh_user: str
    ssh_pass: str


class GitLabConfig(BaseModel):
    ip: str
    ssh_user: str
    become_pass: str
    gitlab_version: str       = "18.9.1"
    gitlab_root_password: str
    worker_ip: str
    worker_nodeport: int      = 32080
    force_redeploy: bool      = False
    seed_group_name: str      = "admins"
    seed_user_username: str   = "testuser"
    seed_user_password: str   = ""


@router.post("/api/gitlab/bootstrap-ssh")
async def gitlab_bootstrap_ssh(req: GitLabSSHRequest):
    """
    Push the controller SSH key to the GitLab VM, configure passwordless sudo,
    and populate the controller known_hosts — all in one session while the
    password is still available. After this runs, Ansible can reach the GitLab
    VM with no interactive prompts of any kind.
    """
    if not SSH_PUB_KEY_PATH.exists():
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(SSH_KEY_PATH), "-N", ""],
            check=True, capture_output=True
        )
    pub_key = SSH_PUB_KEY_PATH.read_text().strip()
    client = None
    try:
        client = get_client_with_password(req.ip, req.ssh_user, req.ssh_pass)

        # ── SSH key setup ──────────────────────────────────────────────────────
        for cmd in [
            "mkdir -p ~/.ssh",
            "chmod 700 ~/.ssh",
            f"echo '{pub_key}' >> ~/.ssh/authorized_keys",
            "sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys",
            "chmod 600 ~/.ssh/authorized_keys",
        ]:
            _, stderr, rc = run_command(client, cmd)
            if rc != 0:
                return {"status": "error",
                        "message": f"SSH key setup failed: {cmd} — {stderr}"}

        # ── Passwordless sudo setup ────────────────────────────────────────────
        # Password is still available here — write the sudoers drop-in while
        # the session is open. Uses sudo -S to read password from stdin.
        _, stderr, rc = run_command(
            client,
            f"echo '{req.ssh_pass}' | sudo -S bash -c "
            f"\"echo '{req.ssh_user} ALL=(ALL) NOPASSWD:ALL' "
            f"> /etc/sudoers.d/ansible-nopasswd && "
            f"chmod 440 /etc/sudoers.d/ansible-nopasswd\""
        )
        if rc != 0:
            return {"status": "error",
                    "message": f"Passwordless sudo setup failed — {stderr}"}

        # ── Verify sudo works without password ─────────────────────────────────
        _, _, verify_rc = run_command(client, "sudo -n whoami")
        if verify_rc != 0:
            return {"status": "error",
                    "message": "Sudo verification failed — sudoers entry may not have applied"}

    except paramiko.AuthenticationException:
        return {"status": "error",
                "message": "Authentication failed — wrong password?"}
    except (socket.timeout, paramiko.SSHException) as exc:
        return {"status": "error", "message": f"Connection failed: {exc}"}
    finally:
        if client:
            client.close()

    # ── Populate controller known_hosts ────────────────────────────────────────
    # Done after closing the paramiko session — uses local ssh-keyscan.
    # Non-fatal: if this fails the sudoers + key push already succeeded.
    from pathlib import Path
    known_hosts_path = Path.home() / ".ssh" / "known_hosts"
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    known_hosts_path.touch(exist_ok=True)
    scan = subprocess.run(
        ["ssh-keyscan", "-H", "-T", "5", req.ip],
        capture_output=True, text=True
    )
    if scan.returncode == 0 and scan.stdout.strip():
        existing = known_hosts_path.read_text()
        for line in scan.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            key_blob = parts[-1] if len(parts) >= 3 else ""
            if key_blob and key_blob not in existing:
                with open(known_hosts_path, "a") as f:
                    f.write(line + "\n")

    return {
        "status":  "ok",
        "message": f"SSH key pushed · passwordless sudo configured · fingerprint recorded for {req.ip}"
    }


@router.post("/api/gitlab/configure")
async def gitlab_configure(cfg: GitLabConfig):
    if GITLAB_OUTPUTS_PATH.exists() and not cfg.force_redeploy:
        return {
            "status": "already_deployed",
            "message": (
                "GitLab credentials already exist. "
                "Re-deploying will rotate the admin token. "
                "Set force_redeploy=true to proceed."
            )
        }

    if not cfg.seed_user_password:
        return {
            "status": "error",
            "message": "Seed user password is required."
        }

    callback_url = (
        f"http://{cfg.worker_ip}:{cfg.worker_nodeport}/hub/oauth_callback"
    )

    inventory = (
        f"[gitlab]\n"
        f"{cfg.ip} ansible_host={cfg.ip} ansible_user={cfg.ssh_user} "
        f"ansible_ssh_private_key_file={SSH_KEY_PATH} "
        f"ansible_python_interpreter=/usr/bin/python3\n"
    )

    vars_content = (
        f"# Generated by k8s-launcher — do not edit manually\n\n"
        f'gitlab_vm_ip:             "{cfg.ip}"\n'
        f'gitlab_external_url:      "http://{cfg.ip}"\n'
        f'gitlab_registry_port:     5050\n'
        f'gitlab_version:           "{cfg.gitlab_version}"\n'
        f'gitlab_root_password:     "{cfg.gitlab_root_password}"\n'
        f'jupyterhub_callback_url:  "{callback_url}"\n'
        f'registry_group_name:      "registry"\n'
        f'notebook_images_project:  "notebook-images"\n'
        f'controller_outputs_path:  "{GITLAB_OUTPUTS_PATH}"\n'
        f'seed_group_name:          "{cfg.seed_group_name}"\n'
        f'seed_user_username:       "{cfg.seed_user_username}"\n'
        f'seed_user_password:       "{cfg.seed_user_password}"\n'
    )

    GITLAB_INVENTORY_PATH.write_text(inventory)
    GITLAB_VARS_PATH.write_text(vars_content)

    GITLAB_BECOME_PATH.write_text(
        f'ansible_become_pass: "{cfg.become_pass}"\n'
    )
    GITLAB_BECOME_PATH.chmod(0o600)

    return {
        "status":       "ok",
        "callback_url": callback_url,
        "files": {
            "inventory": str(GITLAB_INVENTORY_PATH),
            "vars":      str(GITLAB_VARS_PATH),
        }
    }


def _gitlab_stream():
    if not GITLAB_INVENTORY_PATH.exists():
        yield "data: __ERROR__:no_inventory — run /api/gitlab/configure first\n\n"
        return

    # ── Phase 1: deploy GitLab ────────────────────────────────────────────────
    cmd = [
        "ansible-playbook",
        "-i", str(GITLAB_INVENTORY_PATH),
        "site.yml",
        "--extra-vars", f"@{GITLAB_VARS_PATH}",
        "--extra-vars", f"@{GITLAB_BECOME_PATH}",
    ]

    process = subprocess.Popen(
        cmd,
        cwd=str(ANSIBLE_GITLAB_DIR),
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

    if process.returncode != 0:
        yield f"data: __ERROR__:{process.returncode}\n\n"
        return

    # FIX R1 — delete become_pass file after Ansible run completes.
    GITLAB_BECOME_PATH.unlink(missing_ok=True)

    # ── Phase 2: configure containerd on all k8s nodes ───────────────────────
    if not GITLAB_OUTPUTS_PATH.exists():
        yield "data: [WARN] gitlab-outputs.json not found — skipping registry config\n\n"
        yield "data: __DONE__\n\n"
        return

    if not INVENTORY_PATH.exists():
        yield "data: [WARN] k8s inventory not found — skipping registry config\n\n"
        yield "data: __DONE__\n\n"
        return

    try:
        outputs = json.loads(GITLAB_OUTPUTS_PATH.read_text())
        registry_host = outputs.get("gitlab_registry_host", "")
    except Exception as exc:
        yield f"data: [WARN] Could not read gitlab-outputs.json: {exc} — skipping registry config\n\n"
        yield "data: __DONE__\n\n"
        return

    if not registry_host:
        yield "data: [WARN] gitlab_registry_host empty — skipping registry config\n\n"
        yield "data: __DONE__\n\n"
        return

    yield f"data: \n\n"
    yield f"data: ── Configuring containerd registry on all k8s nodes ──\n\n"

    reg_cmd = [
        "ansible-playbook",
        "-i", str(INVENTORY_PATH),
        "configure-registry.yml",
        "--extra-vars", f"gitlab_registry_host={registry_host}",
    ]

    reg_process = subprocess.Popen(
        reg_cmd,
        cwd=str(ANSIBLE_K8S_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    for line in iter(reg_process.stdout.readline, ""):
        stripped = line.rstrip()
        if stripped:
            yield f"data: {stripped}\n\n"

    reg_process.stdout.close()
    reg_process.wait()

    if reg_process.returncode != 0:
        yield f"data: [WARN] Registry config playbook exited {reg_process.returncode} — containerd may need manual fix\n\n"
    else:
        yield "data: Registry configured on all nodes ✓\n\n"

    yield "data: __DONE__\n\n"


@router.get("/api/gitlab/stream")
async def gitlab_deploy_stream():
    return StreamingResponse(
        _gitlab_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/gitlab/outputs")
async def gitlab_outputs():
    if not GITLAB_OUTPUTS_PATH.exists():
        return {"status": "not_found", "message": "Run Deploy GitLab first."}
    data = json.loads(GITLAB_OUTPUTS_PATH.read_text())
    return {
        "status":                "ok",
        "gitlab_url":            data.get("gitlab_url"),
        "registry_host":         data.get("gitlab_registry_host"),
        "oauth_client_id":       data.get("oauth_client_id"),
        "project_id":            data.get("notebook_images_project_id"),
        "deploy_token_user":     data.get("registry_deploy_token_user"),
        "admin_token_set":       bool(data.get("admin_token")),
        "oauth_secret_set":      bool(data.get("oauth_client_secret")),
        "deploy_token_pass_set": bool(data.get("registry_deploy_token_pass")),
    }
