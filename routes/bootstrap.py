"""
routes/bootstrap.py — Phase 0 and Phase 1 of the setup flow.

Phase 0: Install Ansible on the controller machine.
Phase 1: For each cluster node, using the password the user typed ONCE:
  1. Push the controller's SSH public key → node's authorized_keys
     After this the controller can SSH in without a password forever.
  2. Write /etc/sudoers.d/ansible-nopasswd on the node
     After this Ansible's 'become: yes' tasks never prompt for a sudo password.
  3. Verify sudo works without a password (safety check before closing session)
  Then after all nodes are processed:
  4. Run ssh-keyscan for each node → write fingerprints to controller known_hosts
     After this OpenSSH never prompts "Are you sure you want to continue connecting?"
  5. Write ansible.cfg to every Ansible project directory
     This is the second layer of protection — StrictHostKeyChecking=no — so even
     if known_hosts is stale (e.g. a node was rebuilt), Ansible still connects.

The password is used exactly once per node (step 1-3) and never stored anywhere.
"""
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

# Import the shared ansible.cfg writer so bootstrap guarantees
# StrictHostKeyChecking=no is in place before any playbook ever runs.
# This function always overwrites — never skips — so the guarantee holds
# even if the file existed before with wrong content.
from core.ansible_cfg import write_ansible_cfgs

router = APIRouter()


# ── Models ─────────────────────────────────────────────────────────────────────

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

        out, _, rc = run_command(client, "ansible --version 2>/dev/null | head -1")
        if rc == 0 and "ansible" in out.lower():
            return {"status": "ok", "message": f"Already installed on {controller.ip}: {out.strip()}"}

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


# ── Phase 1 helpers ────────────────────────────────────────────────────────────

def _ensure_ssh_key() -> str:
    """
    Generate the controller's ed25519 keypair if it doesn't exist yet.
    Returns the public key string ready to paste into authorized_keys.
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
    Open ONE Paramiko session using the user's password and do three things:

    1. SSH key setup — append controller pubkey to authorized_keys with correct
       permissions so all future connections use key auth (no password).

    2. Passwordless sudo — write a sudoers drop-in so Ansible's 'become: yes'
       tasks never need a password. Uses sudo -S to inject the password via stdin
       since there is no TTY in a Paramiko session.

    3. Sudo verification — confirm the sudoers entry actually took effect before
       we close the session. If this fails, the node is not ready for Ansible.

    The password is used here and nowhere else. It is never stored.
    """
    client = None
    try:
        client = get_client_with_password(ip, ssh_user, ssh_pass)

        # ── 1. SSH key setup ───────────────────────────────────────────────────
        # sort -u deduplicates — safe to run multiple times (idempotent)
        for cmd in [
            "mkdir -p ~/.ssh",
            "chmod 700 ~/.ssh",
            f"echo '{pub_key}' >> ~/.ssh/authorized_keys",
            "sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys",
            "chmod 600 ~/.ssh/authorized_keys",
        ]:
            _, stderr, exit_code = run_command(client, cmd)
            if exit_code != 0:
                return {"ip": ip, "status": "error",
                        "message": f"Command failed: {cmd} — {stderr}"}

        # ── 2. Passwordless sudo setup ─────────────────────────────────────────
        # sudo -S reads the password from stdin (the echo pipe).
        # bash -c runs the two commands as root in a single sudo invocation.
        # chmod 440 is the required permission for sudoers.d drop-in files.
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

        # ── 3. Verify sudo works without password ──────────────────────────────
        # sudo -n is non-interactive — it fails immediately if a password is needed.
        # If this returns rc=0, the sudoers entry is active.
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


def _populate_known_hosts(ips: list) -> list:
    """
    Run ssh-keyscan against each IP and append new fingerprints to
    ~/.ssh/known_hosts on the controller.

    This is the programmatic equivalent of manually SSHing to a new host
    and typing 'yes' at the fingerprint prompt. After this runs, OpenSSH
    will never prompt for these IPs again.

    Deduplication is done by comparing key blobs (the last field in each
    known_hosts line) — so running this multiple times is safe.
    """
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
                # Each known_hosts line: <hashed-host> <key-type> <key-blob>
                # We deduplicate by the key blob (last field) — unique per host+keytype
                key_blob = parts[-1] if len(parts) >= 3 else ""
                if key_blob and key_blob not in existing:
                    with open(known_hosts_path, "a") as f:
                        f.write(line + "\n")
                    added += 1
            results.append({
                "ip":          ip,
                "status":      "ok",
                "known_hosts": f"{added} key(s) written"
            })
        else:
            # Non-fatal — key push already succeeded.
            # ansible.cfg StrictHostKeyChecking=no is the safety net.
            results.append({
                "ip":          ip,
                "status":      "warn",
                "known_hosts": f"ssh-keyscan failed: {scan.stderr.strip() or 'no output'}"
            })
    return results


# ── Phase 1 endpoint ───────────────────────────────────────────────────────────

@router.post("/api/bootstrap/ssh")
async def bootstrap_ssh(request: BootstrapSSHRequest):
    """
    For every node in the request:
      - Push SSH key + configure passwordless sudo (one password session)
    Then for all nodes that succeeded:
      - Populate controller known_hosts (ssh-keyscan)
      - Write ansible.cfg to every Ansible project (StrictHostKeyChecking=no)

    After this endpoint returns OK, the controller can reach every node
    with zero interactive prompts — Ansible is ready to run.
    """
    pub_key = _ensure_ssh_key()

    # ── Push SSH keys + configure passwordless sudo on every node ──────────────
    results = []
    for node in request.nodes:
        result = _push_key_to_node(
            ip=node.ip,
            ssh_user=node.ssh_user,
            ssh_pass=node.ssh_pass,
            pub_key=pub_key
        )
        results.append(result)

    # ── Populate known_hosts for all successfully reached nodes ────────────────
    # Only scan nodes where key push succeeded — no point scanning unreachable ones
    ok_ips = [r["ip"] for r in results if r["status"] == "ok"]
    if ok_ips:
        scan_results = _populate_known_hosts(ok_ips)
        scan_by_ip = {r["ip"]: r for r in scan_results}
        for r in results:
            if r["ip"] in scan_by_ip:
                r["known_hosts"] = scan_by_ip[r["ip"]]["known_hosts"]

    # ── Write ansible.cfg to all Ansible project directories ──────────────────
    # This is the second layer of SSH trust — belt + suspenders.
    # known_hosts handles the normal case; StrictHostKeyChecking=no handles
    # edge cases like a node being rebuilt with a new fingerprint.
    # Always overwrites so stale or missing files can't cause a regression.
    cfgs_written = write_ansible_cfgs()

    failed = [r for r in results if r["status"] == "error"]
    return {
        "status":           "error" if failed else "ok",
        "results":          results,
        "ansible_cfgs":     len(cfgs_written)
    }
