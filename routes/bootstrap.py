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

        # ── SSH key setup ──────────────────────────────────────────────────────
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

        # ── Passwordless sudo setup ────────────────────────────────────────────
        # We are still authenticated with the user's password at this point so
        # we can write the sudoers drop-in while the session is open.
        # Uses echo piped through tee (no heredoc) to avoid shell quoting issues.
        # The chmod 440 matches the standard sudoers.d permission requirement.
        sudo_commands = [
            f"echo '{ssh_user} ALL=(ALL) NOPASSWD:ALL' | sudo -S tee /etc/sudoers.d/ansible-nopasswd",
            "sudo chmod 440 /etc/sudoers.d/ansible-nopasswd",
        ]
        # Pass the password via stdin for the first sudo call (-S flag)
        _, stderr, exit_code = run_command(
            client,
            f"echo '{ssh_pass}' | sudo -S bash -c "
            f"\"echo '{ssh_user} ALL=(ALL) NOPASSWD:ALL' "
            f"> /etc/sudoers.d/ansible-nopasswd && "
            f"chmod 440 /etc/sudoers.d/ansible-nopasswd\""
        )
        if exit_code != 0:
            return {"ip": ip, "status": "error",
                    "message": f"Passwordless sudo setup failed — {stderr}"}

        # ── Verify sudo works without password now ─────────────────────────────
        _, _, verify_rc = run_command(client, "sudo -n whoami")
        if verify_rc != 0:
            return {"ip": ip, "status": "error",
                    "message": "Sudo verification failed — sudoers entry may not have applied"}

        return {
            "ip":     ip,
            "status": "ok",
            "message": "SSH key pushed · passwordless sudo configured"
        }

    except paramiko.AuthenticationException:
        return {"ip": ip, "status": "error",
                "message": "Authentication failed — wrong password?"}
    except (socket.timeout, paramiko.SSHException) as exc:
        return {"ip": ip, "status": "error",
                "message": f"Connection failed: {exc}"}
    finally:
        if client:
            client.close()


# ── Populate system known_hosts via ssh-keyscan ────────────────────────────────
# Runs after key push so Ansible's OpenSSH client never encounters an unknown
# host fingerprint and never prompts interactively.

def _populate_known_hosts(ips: list) -> list:
    known_hosts_path = Path.home() / ".ssh" / "known_hosts"
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    known_hosts_path.touch(exist_ok=True)

    results = []
    for ip in ips:
        scan = subprocess.run(
            ["ssh-keyscan", "-H", "-T", "5", ip],
            capture_output=True, text=True
        )
        if scan.returncode == 0 and scan.stdout.strip():
            existing = known_hosts_path.read_text()
            added = 0
            for line in scan.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                key_blob = parts[-1] if len(parts) >= 3 else ""
                if key_blob and key_blob not in existing:
                    with open(known_hosts_path, "a") as f:
                        f.write(line + "\n")
                    added += 1
            results.append({
                "ip":           ip,
                "status":       "ok",
                "known_hosts":  f"{added} key(s) written"
            })
        else:
            results.append({
                "ip":           ip,
                "status":       "warn",
                "known_hosts":  f"ssh-keyscan failed: {scan.stderr.strip() or 'no output'}"
            })
    return results


# ── Endpoint ───────────────────────────────────────────────────────────────────

@router.post("/api/bootstrap/ssh")
async def bootstrap_ssh(request: BootstrapSSHRequest):
    pub_key = _ensure_ssh_key()

    # Push SSH keys + configure passwordless sudo on every node
    results = []
    for node in request.nodes:
        result = _push_key_to_node(
            ip=node.ip,
            ssh_user=node.ssh_user,
            ssh_pass=node.ssh_pass,
            pub_key=pub_key
        )
        results.append(result)

    # Populate known_hosts for all successfully reached nodes
    ok_ips = [r["ip"] for r in results if r["status"] == "ok"]
    if ok_ips:
        scan_results = _populate_known_hosts(ok_ips)
        scan_by_ip = {r["ip"]: r for r in scan_results}
        for r in results:
            if r["ip"] in scan_by_ip:
                r["known_hosts"] = scan_by_ip[r["ip"]]["known_hosts"]

    failed = [r for r in results if r["status"] == "error"]
    return {"status": "error" if failed else "ok", "results": results}
