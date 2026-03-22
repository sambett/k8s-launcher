import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import List

import paramiko
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.paths import SSH_KEY_PATH, SSH_PUB_KEY_PATH
from core.ssh import get_client_with_password, run_command

router = APIRouter()


# ── Models ─────────────────────────────────────────────────────────────────────

class NodeEntry(BaseModel):
    ip: str
    hostname: str
    ssh_user: str
    ssh_pass: str          # held in memory only, never persisted


class BootstrapSSHRequest(BaseModel):
    nodes: List[NodeEntry]


# ── Phase 0 — Install Ansible ──────────────────────────────────────────────────

@router.post("/api/bootstrap/ansible")
async def install_ansible():
    """
    Check if Ansible is installed on the controller.
    Install via pip3 --user if not found.
    """
    if shutil.which("ansible"):
        check = subprocess.run(
            ["ansible", "--version"],
            capture_output=True, text=True
        )
        version_line = check.stdout.splitlines()[0]
        return {"status": "ok", "message": f"Already installed: {version_line}"}

    install = subprocess.run(
        ["pip3", "install", "--user", "ansible"],
        capture_output=True, text=True
    )
    if install.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Ansible install failed:\n{install.stderr}"
        )

    local_bin = str(Path.home() / ".local" / "bin")
    os.environ["PATH"] = local_bin + ":" + os.environ.get("PATH", "")

    verify = subprocess.run(
        ["ansible", "--version"],
        capture_output=True, text=True
    )
    version_line = (
        verify.stdout.splitlines()[0] if verify.returncode == 0 else "unknown"
    )
    return {"status": "ok", "message": f"Installed: {version_line}"}


# ── Phase 1 — SSH Key Bootstrap ────────────────────────────────────────────────

def _ensure_ssh_key() -> str:
    """
    Generate ed25519 key pair on controller if absent.
    Returns the public key string.
    """
    if not SSH_KEY_PATH.exists():
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519",
             "-f", str(SSH_KEY_PATH), "-N", ""],
            check=True, capture_output=True
        )
    return SSH_PUB_KEY_PATH.read_text().strip()


def _push_key_to_node(ip: str, ssh_user: str,
                       ssh_pass: str, pub_key: str) -> dict:
    """
    Connect with password once, append public key to authorized_keys.
    Password lives in local scope only — discarded on return.
    """
    client = None
    try:
        client = get_client_with_password(ip, ssh_user, ssh_pass)

        commands = [
            "mkdir -p ~/.ssh",
            "chmod 700 ~/.ssh",
            f"echo '{pub_key}' >> ~/.ssh/authorized_keys",
            "sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys",
            "chmod 600 ~/.ssh/authorized_keys",
        ]

        for cmd in commands:
            _, stderr, exit_code = run_command(client, cmd)
            if exit_code != 0:
                return {
                    "ip": ip,
                    "status": "error",
                    "message": f"Command failed: {cmd} — {stderr}"
                }

        return {"ip": ip, "status": "ok", "message": "SSH key pushed"}

    except paramiko.AuthenticationException:
        return {"ip": ip, "status": "error",
                "message": "Authentication failed — wrong password?"}
    except (socket.timeout, paramiko.SSHException) as exc:
        return {"ip": ip, "status": "error",
                "message": f"Connection failed: {exc}"}
    finally:
        if client:
            client.close()


@router.post("/api/bootstrap/ssh")
async def bootstrap_ssh(request: BootstrapSSHRequest):
    """
    Push controller's ed25519 public key to every node.
    Passwords are used only in memory and never persisted.
    """
    pub_key = _ensure_ssh_key()
    results = []

    for node in request.nodes:
        result = _push_key_to_node(
            ip=node.ip,
            ssh_user=node.ssh_user,
            ssh_pass=node.ssh_pass,
            pub_key=pub_key
        )
        results.append(result)

    failed = [r for r in results if r["status"] == "error"]
    return {"status": "error" if failed else "ok", "results": results}
