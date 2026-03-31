import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import List, Optional

import paramiko
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.paths import SSH_KEY_PATH, SSH_PUB_KEY_PATH
from core.ssh import get_client_with_password, get_client_with_key, run_command

router = APIRouter()


class NodeEntry(BaseModel):
    ip: str
    hostname: str
    ssh_user: str
    ssh_pass: str


class BootstrapSSHRequest(BaseModel):
    nodes: List[NodeEntry]


class ControllerEntry(BaseModel):
    ip: str
    ssh_user: str
    ssh_pass: str
    is_local: bool = False   # True when the launcher IS running on this machine


# ── Phase 0 — Install Ansible ──────────────────────────────────────────────────

@router.post("/api/bootstrap/ansible")
async def install_ansible(controller: Optional[ControllerEntry] = None):
    """
    Install Ansible on the controller machine.
    If controller.is_local is True (or no controller given), install on this machine.
    Otherwise SSH into the specified machine and install there.
    """

    # ── Local install ──────────────────────────────────────────────────────────
    if controller is None or controller.is_local:
        if shutil.which("ansible"):
            check = subprocess.run(
                ["ansible", "--version"], capture_output=True, text=True
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
            ["ansible", "--version"], capture_output=True, text=True
        )
        version_line = (
            verify.stdout.splitlines()[0] if verify.returncode == 0 else "unknown"
        )
        return {"status": "ok", "message": f"Installed: {version_line}"}

    # ── Remote install via SSH ─────────────────────────────────────────────────
    client = None
    try:
        client = get_client_with_password(
            controller.ip, controller.ssh_user, controller.ssh_pass
        )

        # Check if already installed
        out, _, rc = run_command(client, "ansible --version 2>/dev/null | head -1")
        if rc == 0 and "ansible" in out.lower():
            return {"status": "ok", "message": f"Already installed on {controller.ip}: {out.strip()}"}

        # Install
        _, stderr, rc = run_command(
            client,
            "pip3 install --user ansible 2>&1 && "
            "echo 'export PATH=$HOME/.local/bin:$PATH' >> ~/.bashrc"
        )
        if rc != 0:
            return {"status": "error",
                    "message": f"Install failed on {controller.ip}: {stderr}"}

        out, _, _ = run_command(
            client,
            "~/.local/bin/ansible --version 2>/dev/null | head -1 || "
            "ansible --version 2>/dev/null | head -1"
        )
        return {
            "status":  "ok",
            "message": f"Installed on {controller.ip}: {out.strip()}"
        }

    except paramiko.AuthenticationException:
        return {"status": "error",
                "message": f"Authentication failed for {controller.ip} — wrong password?"}
    except (socket.timeout, paramiko.SSHException) as exc:
        return {"status": "error",
                "message": f"Cannot reach {controller.ip}: {exc}"}
    finally:
        if client:
            client.close()


# ── Phase 1 — SSH Key Bootstrap ────────────────────────────────────────────────

def _ensure_ssh_key() -> str:
    if not SSH_KEY_PATH.exists():
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519",
             "-f", str(SSH_KEY_PATH), "-N", ""],
            check=True, capture_output=True
        )
    return SSH_PUB_KEY_PATH.read_text().strip()


def _push_key_to_node(ip: str, ssh_user: str,
                      ssh_pass: str, pub_key: str) -> dict:
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
                return {"ip": ip, "status": "error",
                        "message": f"Command failed: {cmd} — {stderr}"}
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
