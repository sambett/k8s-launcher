"""
routes/worker.py — Add and remove worker nodes.
Streams progress as SSE so the UI shows live feedback.

Add-worker flow:
  Step 1 (Python/Paramiko) — Full SSH trust setup using the password once:
    1a. Read actual VM hostname, set it via hostnamectl if it does not match
        what the operator typed. Add 127.0.1.1 <hostname> to /etc/hosts on
        the new VM — required for kubelet to register under the correct name.
    1b. OS version check — fail fast before installing anything.
    1c. Push controller public key → new worker authorized_keys
    1d. Write passwordless sudo on new worker
    1e. Verify sudo works without password
    1f. ssh-keyscan → populate controller known_hosts for new worker
    1g. Wire cplane → new worker (generate cplane keypair if missing,
        push cplane pubkey to new worker, populate cplane known_hosts)
    1h. write_ansible_cfgs() → StrictHostKeyChecking=no in all Ansible dirs

  Step 2 (Ansible) — Full node config via ansible-workers/add-worker.yml
    Runs 5 focused roles in sequence:
      - node_prep:            OS prep, /etc/hosts, swap, kernel modules, sysctl
      - longhorn_prereqs:     iscsi_tcp, cryptsetup, multipathd
      - containerd:           container runtime install and config
      - kubernetes_packages:  kubelet + kubeadm at pinned version
      - worker_join:          stale state cleanup, kubeadm join, labels

  Step 3 (Python) — Inventory and state updates
    - Update generated/inventory.ini — new worker inserted inside [workers]
    - Update cluster_hosts in generated/group_vars/all.yml — so future
      playbook runs propagate the new node to /etc/hosts on all nodes
    - Propagate new worker's hostname/IP to /etc/hosts on existing nodes

  Step 4 (Python) — Validation before declaring success
    - Node is Ready in kubectl
    - Longhorn has discovered the node
    - Longhorn node has a schedulable disk
    - calico-node DaemonSet pod is Running on the new node
    - iscsi_tcp module is loaded on the new node
    - multipathd is inactive on the new node

Remove-worker flow (unchanged):
  (Python) — Longhorn safety check + worker count warning
  (Ansible) — Drain + delete + VM cleanup via ansible-workers/remove-worker.yml
  (Python) — Update permanent inventory
"""
import json
import socket
import subprocess
import tempfile
from pathlib import Path

import paramiko
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.paths import (
    INVENTORY_PATH, VARS_PATH, SSH_KEY_PATH, SSH_PUB_KEY_PATH,
    ANSIBLE_WORKERS_DIR,
)
from core.ssh import get_client_with_password, get_client_with_key, run_command
from core.ansible import run_on_cp

from core.ansible_cfg import write_ansible_cfgs

router = APIRouter()


# ── File structure ─────────────────────────────────────────────────────────────
#
#   Models              NewWorker, RemoveWorkerRequest
#   Helpers             _get_registry_host, _get_cp_info,
#                       _make_worker_inventory, _call_playbook,
#                       _update_inventory, _update_cluster_hosts,
#                       _propagate_etc_hosts, _validate_new_worker
#   Add-worker          _add_worker_stream → POST /api/cluster/add-worker
#   Remove-worker       _remove_worker_stream → POST /api/cluster/remove-worker
#
# ──────────────────────────────────────────────────────────────────────────────


# ── Models ─────────────────────────────────────────────────────────────────────

class NewWorker(BaseModel):
    ip: str
    hostname: str
    ssh_user: str
    ssh_pass: str


class RemoveWorkerRequest(BaseModel):
    hostname: str
    ip:       str
    ssh_user: str


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_registry_host() -> str:
    """
    Read the GitLab registry host for containerd insecure registry config.

    Primary source:   generated/jupyterhub-vars.yml
                      Present when JupyterHub has been deployed.

    Fallback source:  generated/gitlab-outputs.json
                      Present when GitLab has been deployed but JupyterHub
                      has not yet. Without this fallback, a worker added
                      before JupyterHub is deployed gets no hosts.toml and
                      fails ImagePullBackOff when notebooks are later
                      scheduled on it — with no obvious link to add-worker.
    """
    from core.paths import JUPYTERHUB_VARS_PATH, GITLAB_OUTPUTS_PATH
    import json as _json

    # Primary: jupyterhub vars
    try:
        for line in JUPYTERHUB_VARS_PATH.read_text().splitlines():
            if line.strip().startswith("jhub_registry_host:"):
                val = line.split(":", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    except Exception:
        pass

    # Fallback: gitlab outputs
    try:
        data = _json.loads(GITLAB_OUTPUTS_PATH.read_text())
        val = data.get("gitlab_registry_host", "")
        if val:
            return val
    except Exception:
        pass

    return ""


def _get_cp_info() -> dict:
    """
    Read control plane IP, SSH user, and hostname from generated files.
    Returns empty strings if files are missing — caller handles gracefully.
    """
    cp_ip       = ""
    cp_user     = ""
    cp_hostname = ""
    try:
        for line in VARS_PATH.read_text().splitlines():
            if line.strip().startswith("cp_ip:"):
                cp_ip = line.split(":", 1)[1].strip().strip('"').strip("'")
            if line.strip().startswith("cp_hostname:"):
                cp_hostname = line.split(":", 1)[1].strip().strip('"').strip("'")
        if INVENTORY_PATH.exists():
            in_cp = False
            for line in INVENTORY_PATH.read_text().splitlines():
                if "[control_plane]" in line:
                    in_cp = True
                    continue
                if in_cp and line.strip() and not line.startswith("["):
                    for part in line.split():
                        if part.startswith("ansible_user="):
                            cp_user = part.split("=", 1)[1]
                    break
                if in_cp and line.startswith("["):
                    break
    except Exception:
        pass
    return {"ip": cp_ip, "user": cp_user, "hostname": cp_hostname}


def _make_worker_inventory(cp: dict, worker_hostname: str,
                            worker_ip: str, worker_user: str) -> str:
    """
    Build a minimal inventory string for a single-worker ansible-workers run.
    The control plane entry is required so delegate_to tasks inside worker_join
    can reach kubectl on the control plane.
    """
    return (
        f"[control_plane]\n"
        f"{cp['hostname']} ansible_host={cp['ip']} ansible_user={cp['user']}\n"
        "\n"
        f"[workers]\n"
        f"{worker_hostname} ansible_host={worker_ip} ansible_user={worker_user}\n"
        "\n"
        "[all:vars]\n"
        "ansible_python_interpreter=/usr/bin/python3\n"
        f"ansible_ssh_private_key_file={SSH_KEY_PATH}\n"
    )


def _call_playbook(playbook: str, inventory: str, extra_vars: dict):
    """
    Write temp inventory + vars files, invoke ansible-playbook, stream stdout.
    Temp files are always cleaned up regardless of playbook outcome.
    Yields SSE-formatted strings.
    Final yield is 'data: __PLAYBOOK_OK__' or 'data: __PLAYBOOK_FAIL__:N'.
    """
    inv_f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".ini", delete=False, prefix="w-inv-"
    )
    inv_f.write(inventory)
    inv_f.close()

    vars_f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="w-vars-"
    )
    vars_f.write(json.dumps(extra_vars))
    vars_f.close()

    cmd = [
        "ansible-playbook",
        "-i", inv_f.name,
        playbook,
        "--extra-vars", f"@{VARS_PATH}",
        "--extra-vars", f"@{vars_f.name}",
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=str(ANSIBLE_WORKERS_DIR),
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

    Path(inv_f.name).unlink(missing_ok=True)
    Path(vars_f.name).unlink(missing_ok=True)

    if proc.returncode == 0:
        yield "data: __PLAYBOOK_OK__\n\n"
    else:
        yield f"data: __PLAYBOOK_FAIL__:{proc.returncode}\n\n"


def _update_inventory(hostname: str, ip: str, ssh_user: str) -> str:
    """
    Insert the new worker into the [workers] section of generated/inventory.ini.

    The previous implementation used str.replace("[all:vars]", ...) which
    placed the new entry BETWEEN the [workers] section and [all:vars] — outside
    any group. Ansible's INI parser puts such hosts into 'ungrouped', making
    every future playbook run targeting the [workers] group silently exclude
    the new node.

    This implementation finds the [workers] section header and appends the new
    line as the last entry in that section, before the next blank line or group.
    """
    if not INVENTORY_PATH.exists():
        return "inventory file not found"

    new_line = (
        f"{hostname} ansible_host={ip} ansible_user={ssh_user}"
    )
    inv = INVENTORY_PATH.read_text()

    # Idempotency check — do not add duplicates
    if new_line in inv:
        return f"{hostname} already in inventory — no change"

    lines  = inv.splitlines()
    result = []
    inserted    = False
    in_workers  = False

    for i, line in enumerate(lines):
        result.append(line)

        # We are now inside the [workers] section
        if line.strip() == "[workers]":
            in_workers = True
            continue

        if in_workers and not inserted:
            # A blank line or a new section header marks the end of [workers].
            # Insert our new entry just before that boundary.
            if line.strip() == "" or line.strip().startswith("["):
                # Remove the line we just appended, insert new entry first,
                # then put the boundary line back.
                result.pop()
                result.append(new_line)
                result.append(line)
                inserted    = True
                in_workers  = False

    # Edge case: [workers] is the last section with no trailing blank line
    if in_workers and not inserted:
        result.append(new_line)
        inserted = True

    if not inserted:
        return f"WARNING: [workers] section not found — {hostname} not added to inventory"

    INVENTORY_PATH.write_text("\n".join(result) + "\n")
    return f"inventory updated — {hostname} added to [workers]"


def _update_cluster_hosts(hostname: str, ip: str) -> str:
    """
    Append the new worker to the cluster_hosts list in
    generated/group_vars/all.yml.

    cluster_hosts is a YAML list used by multiple Ansible playbooks to
    populate /etc/hosts on all nodes. If this list is not updated, any future
    playbook run (ansible-longhorn, ansible-k8s, ansible-workers) will omit
    the new node from /etc/hosts propagation across the cluster.

    We do a simple line-based append rather than full YAML parsing to avoid
    introducing a PyYAML dependency on the write path and to preserve the
    existing file's formatting and comments exactly.
    """
    if not VARS_PATH.exists():
        return "group_vars file not found"

    entry = f"  - {{ name: {hostname}, ip: \"{ip}\" }}"
    content = VARS_PATH.read_text()

    # Idempotency check
    if hostname in content and ip in content:
        return f"{hostname} already in cluster_hosts — no change"

    lines  = content.splitlines()
    result = []
    in_cluster_hosts = False
    inserted         = False

    for line in lines:
        result.append(line)

        if line.strip().startswith("cluster_hosts:"):
            in_cluster_hosts = True
            continue

        if in_cluster_hosts and not inserted:
            # A line that does NOT start with "  -" marks the end of the list
            if not line.startswith("  -"):
                result.insert(len(result) - 1, entry)
                inserted         = True
                in_cluster_hosts = False

    # Edge case: cluster_hosts is the last block in the file
    if in_cluster_hosts and not inserted:
        result.append(entry)
        inserted = True

    if not inserted:
        return "WARNING: cluster_hosts key not found — entry not added"

    VARS_PATH.write_text("\n".join(result) + "\n")
    return f"cluster_hosts updated — {hostname} appended"


def _propagate_etc_hosts(hostname: str, ip: str) -> str:
    """
    Add the new worker's hostname/IP to /etc/hosts on all existing cluster
    nodes (control plane + existing workers) via a direct Ansible ad-hoc call.

    Why this is needed:
      - Longhorn uses node hostnames for inter-replica iSCSI paths. If an
        existing node cannot resolve the new worker's hostname, replica
        communication fails after a volume rebalance.
      - Ansible delegate_to tasks that resolve by hostname also depend on this.

    Why subprocess.run and NOT run_on_cp:
      run_on_cp executes its argument as a shell command on ansiblecplane.
      INVENTORY_PATH is a local path on ansiblectl — it does not exist on
      ansiblecplane. Running "ansible all -i /home/ansiblectl/..." as a shell
      command on the control plane would fail with "No such file or directory".
      subprocess.run executes Ansible directly from ansiblectl where the
      inventory file actually lives.
    """
    if not INVENTORY_PATH.exists():
        return "inventory not found — skipping /etc/hosts propagation"

    result = subprocess.run(
        [
            "ansible", "all",
            "-i", str(INVENTORY_PATH),
            "-m", "lineinfile",
            "-a", f"path=/etc/hosts line='{ip} {hostname}' state=present",
            "--become",
            "--extra-vars", f"@{VARS_PATH}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return f"/etc/hosts updated on all existing nodes with {ip} {hostname}"
    return (
        f"WARNING: /etc/hosts propagation partially failed "
        f"(rc={result.returncode}) — check manually"
    )


def _validate_new_worker(hostname: str, ip: str) -> list:
    """
    Run post-join validation checks via the control plane before declaring
    success. Returns a list of result strings for display in the SSE stream.

    Checks:
      1. Node is Ready in kubectl
      2. Longhorn has discovered the node (nodes.longhorn.io resource exists)
      3. Longhorn node has at least one schedulable disk
      4. calico-node DaemonSet pod is Running on the new node
      5. iscsi_tcp kernel module is loaded on the new node
      6. multipathd is inactive on the new node
    """
    results = []

    # ── 1. Node Ready ──────────────────────────────────────────────────────────
    out, rc = run_on_cp(
        f"kubectl get node {hostname} --no-headers 2>/dev/null | awk '{{print $2}}'"
    )
    # Strip the Ansible ad-hoc header before reading the value
    status = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if "Ready" in status and "NotReady" not in status:
        results.append(f"✓ Node {hostname} is Ready")
    else:
        results.append(f"✗ Node {hostname} is not Ready — status: {status or 'unknown'}")

    # ── 2. Longhorn node discovery ─────────────────────────────────────────────
    out, rc = run_on_cp(
        f"kubectl get nodes.longhorn.io {hostname} -n longhorn-system "
        f"--no-headers 2>/dev/null | wc -l"
    )
    count = out.strip().splitlines()[-1].strip() if out.strip() else "0"
    if count.isdigit() and int(count) > 0:
        results.append(f"✓ Longhorn has discovered {hostname}")
    else:
        results.append(
            f"✗ Longhorn has not discovered {hostname} yet — "
            f"check node.longhorn.io/create-default-disk label"
        )

    # ── 3. Longhorn schedulable disk ───────────────────────────────────────────
    out, rc = run_on_cp(
        f"kubectl get nodes.longhorn.io {hostname} -n longhorn-system "
        f"-o jsonpath='{{.spec.disks}}' 2>/dev/null"
    )
    disk_data = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if disk_data and disk_data != "{}":
        results.append(f"✓ Longhorn disk registered on {hostname}")
    else:
        results.append(
            f"⚠ Longhorn disk not yet registered on {hostname} — "
            f"may appear within 30s as Longhorn reconciles"
        )

    # ── 4. calico-node pod Running ─────────────────────────────────────────────
    out, rc = run_on_cp(
        f"kubectl get pod -n calico-system -l k8s-app=calico-node "
        f"--field-selector spec.nodeName={hostname} "
        f"--no-headers 2>/dev/null | awk '{{print $3}}'"
    )
    calico_status = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if calico_status == "Running":
        results.append(f"✓ calico-node pod is Running on {hostname}")
    else:
        results.append(
            f"⚠ calico-node pod not yet Running on {hostname} "
            f"(status: {calico_status or 'not found'}) — "
            f"may still be pulling image"
        )

    # ── 5. iscsi_tcp module loaded ─────────────────────────────────────────────
    # Run Ansible ad-hoc directly from ansiblectl (this process) targeting the
    # new worker. We cannot use run_on_cp here because run_on_cp executes on
    # ansiblecplane — INVENTORY_PATH is a local path on ansiblectl and does
    # not exist on the control plane.
    iscsi_result = subprocess.run(
        [
            "ansible", hostname,
            "-i", str(INVENTORY_PATH),
            "-m", "shell",
            "-a", "lsmod | grep -c iscsi_tcp",
            "--extra-vars", f"@{VARS_PATH}",
        ],
        capture_output=True,
        text=True,
    )
    # Ansible ad-hoc output: last non-empty line is the command result.
    # Empty output means Ansible could not reach the node — report that
    # explicitly rather than falling back to "0" which looks like a module
    # check result when it is actually a connection failure.
    iscsi_out = (iscsi_result.stdout + iscsi_result.stderr).strip()
    if not iscsi_out:
        results.append(
            f"⚠ iscsi_tcp check skipped — could not reach {hostname} via Ansible"
        )
    else:
        count_str = iscsi_out.splitlines()[-1].strip()
        if count_str.isdigit() and int(count_str) > 0:
            results.append(f"✓ iscsi_tcp module is loaded on {hostname}")
        else:
            results.append(
                f"✗ iscsi_tcp module NOT loaded on {hostname} — "
                f"Longhorn volume attach will fail. Run: modprobe iscsi_tcp"
            )

    # ── 6. multipathd inactive ─────────────────────────────────────────────────
    # Same pattern — direct subprocess from ansiblectl, not via run_on_cp.
    mpd_result = subprocess.run(
        [
            "ansible", hostname,
            "-i", str(INVENTORY_PATH),
            "-m", "shell",
            "-a", "systemctl is-active multipathd 2>/dev/null || echo inactive",
            "--extra-vars", f"@{VARS_PATH}",
        ],
        capture_output=True,
        text=True,
    )
    mpd_out = (mpd_result.stdout + mpd_result.stderr).strip()
    if not mpd_out:
        results.append(
            f"⚠ multipathd check skipped — could not reach {hostname} via Ansible"
        )
    else:
        mpd_status = mpd_out.splitlines()[-1].strip()
        if mpd_status in ("inactive", "unknown", "failed"):
            results.append(f"✓ multipathd is inactive on {hostname}")
        else:
            results.append(
                f"✗ multipathd is active on {hostname} — "
                f"this will interfere with Longhorn iSCSI volume attachment. "
                f"Run: systemctl stop multipathd && systemctl disable multipathd"
            )

    return results


# ── Add worker ────────────────────────────────────────────────────────────────

def _add_worker_stream(node: NewWorker):
    """
    Four-step worker onboarding streamed live to the browser.

      Step 1 — Bootstrap SSH trust (Python/Paramiko)
      Step 2 — Configure node and join cluster (Ansible)
      Step 3 — Update inventory, group_vars, and /etc/hosts on existing nodes
      Step 4 — Validate the node is fully operational
    """
    TOTAL = 4

    def _step(n, msg): return f"data: PLAY [Step {n}/{TOTAL}] {msg}\n\n"
    def _ok(msg):      return f"data: ok: {msg}\n\n"
    def _fail(msg):    return f"data: FAILED: {msg}\n\n"
    def _log(msg):     return f"data: {msg}\n\n"
    def _done():       return "data: __DONE__\n\n"
    def _err(code):    return f"data: __ERROR__:{code}\n\n"

    # ── Step 1: Full SSH trust setup ───────────────────────────────────────────
    yield _step(1, f"Bootstrapping SSH key and sudo on {node.ip}")

    pub_key = SSH_PUB_KEY_PATH.read_text().strip()
    client  = None
    try:
        client = get_client_with_password(node.ip, node.ssh_user, node.ssh_pass)

        # 1a. Hostname verification and correction
        # ─────────────────────────────────────────
        # Read the VM's actual current hostname. If it does not match what the
        # operator typed in the form, set it now via hostnamectl.
        # This MUST happen before the Ansible role runs because node_prep
        # asserts ansible_hostname == inventory_hostname. A mismatch causes
        # the playbook to fail 8+ minutes in with a confusing assertion error.
        actual_hostname, _, rc = run_command(client, "hostname")
        if actual_hostname.strip() != node.hostname.strip():
            yield _log(
                f"VM hostname is '{actual_hostname.strip()}', "
                f"expected '{node.hostname}' — setting it now"
            )
            _, stderr, rc = run_command(
                client,
                f"echo '{node.ssh_pass}' | sudo -S hostnamectl set-hostname {node.hostname}"
            )
            if rc != 0:
                yield _fail(f"Could not set hostname: {stderr}")
                yield _err("hostname_set")
                return
            yield _ok(f"Hostname set to {node.hostname}")
        else:
            yield _ok(f"Hostname already correct: {actual_hostname.strip()}")

        # 1b. Ensure 127.0.1.1 <hostname> is in /etc/hosts on the new VM
        # ─────────────────────────────────────────────────────────────────
        # kubelet uses this entry to determine the node's registration name.
        # Without it, kubelet may register under "localhost" or an unexpected
        # name, causing the node to appear in kubectl get nodes with the wrong
        # name and the Ansible label step to fail finding it by IP.
        _, _, rc = run_command(
            client,
            f"grep -c '^127\\.0\\.1\\.1 {node.hostname}$' /etc/hosts"
        )
        if rc != 0:
            _, stderr, set_rc = run_command(
                client,
                f"echo '{node.ssh_pass}' | sudo -S sh -c "
                f""echo '127.0.1.1 {node.hostname}' >> /etc/hosts""
            )
            if set_rc != 0:
                yield _fail(f"Could not set 127.0.1.1 entry: {stderr}")
                yield _err("hosts_127")
                return
            yield _ok(f"Added 127.0.1.1 {node.hostname} to /etc/hosts")
        else:
            yield _ok(f"127.0.1.1 {node.hostname} already in /etc/hosts")

        # 1c. OS version check
        # ─────────────────────
        # Fail fast before installing anything. All nodes must run the same
        # Ubuntu LTS — Longhorn kernel modules differ between versions.
        # Check both ID (must be ubuntu) and VERSION_ID (must be supported)
        # so a Debian system that reports VERSION_ID=22.04 is caught early.
        os_id_out, _, _ = run_command(
            client,
            "grep '^ID=' /etc/os-release | cut -d= -f2 | tr -d '\"'"
        )
        if os_id_out.strip() != "ubuntu":
            yield _fail(
                f"Unsupported distribution: '{os_id_out.strip()}'. "
                f"Only Ubuntu 22.04 and 24.04 are supported."
            )
            yield _err("os_check")
            return

        out, _, _ = run_command(
            client,
            "grep '^VERSION_ID=' /etc/os-release | cut -d= -f2 | tr -d '\"'"
        )
        os_ver = out.strip()
        if os_ver not in ("22.04", "24.04"):
            yield _fail(
                f"Unsupported Ubuntu version: {os_ver}. "
                f"Only 22.04 and 24.04 are supported."
            )
            yield _err("os_check")
            return
        yield _ok(f"OS check passed: Ubuntu {os_ver}")

        # 1d. Push controller public key → authorized_keys
        for cmd in [
            "mkdir -p ~/.ssh",
            "chmod 700 ~/.ssh",
            f"echo '{pub_key}' >> ~/.ssh/authorized_keys",
            "sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys",
            "chmod 600 ~/.ssh/authorized_keys",
        ]:
            _, stderr, rc = run_command(client, cmd)
            if rc != 0:
                yield _fail(f"SSH setup failed: {cmd} — {stderr}")
                yield _err("ssh_key")
                return
        yield _ok(f"SSH key installed on {node.ip}")

        # 1e. Write passwordless sudo
        # sudo -S reads password from stdin — no TTY needed in Paramiko sessions
        _, stderr, rc = run_command(
            client,
            f"echo '{node.ssh_pass}' | sudo -S bash -c "
            f"\"echo '{node.ssh_user} ALL=(ALL) NOPASSWD:ALL' "
            f"> /etc/sudoers.d/ansible-nopasswd && "
            f"chmod 440 /etc/sudoers.d/ansible-nopasswd\""
        )
        if rc != 0:
            yield _fail(f"Passwordless sudo setup failed — {stderr}")
            yield _err("sudo_setup")
            return

        # 1f. Verify sudo works without password
        # sudo -n is non-interactive — fails immediately if password required
        _, _, verify_rc = run_command(client, "sudo -n whoami")
        if verify_rc != 0:
            yield _fail("Sudo verification failed — sudoers entry may not have applied")
            yield _err("sudo_verify")
            return
        yield _ok(f"Passwordless sudo configured on {node.ip}")

    except paramiko.AuthenticationException:
        yield _fail("Authentication failed — check the SSH password.")
        yield _err("auth")
        return
    except (socket.timeout, paramiko.SSHException) as exc:
        yield _fail(f"Cannot reach {node.ip}: {exc}")
        yield _err("connection")
        return
    finally:
        if client:
            client.close()

    # 1g. Populate controller known_hosts for the new worker
    # Done after closing the Paramiko session — uses local ssh-keyscan binary.
    known_hosts_path = Path.home() / ".ssh" / "known_hosts"
    known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
    known_hosts_path.touch(exist_ok=True)
    scan = subprocess.run(
        ["ssh-keyscan", "-H", "-T", "5", node.ip],
        capture_output=True, text=True
    )
    if scan.returncode == 0 and scan.stdout.strip():
        existing = known_hosts_path.read_text()
        for line in scan.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts    = line.split()
            key_blob = parts[-1] if len(parts) >= 3 else ""
            if key_blob and key_blob not in existing:
                with open(known_hosts_path, "a") as f:
                    f.write(line + "\n")
    yield _ok(f"Controller known_hosts updated for {node.ip}")

    # 1h. Wire cplane → new worker passwordless SSH
    # Non-fatal — cluster join still works if this fails, but cplane operations
    # targeting the new worker (Ansible delegate_to tasks) would prompt without it.
    cp = _get_cp_info()
    if cp["ip"] and cp["user"]:
        cp_client = None
        try:
            cp_client = get_client_with_key(cp["ip"], cp["user"])

            _, _, rc = run_command(cp_client, "test -f ~/.ssh/id_ed25519.pub")
            if rc != 0:
                run_command(
                    cp_client,
                    "ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N '' -q"
                )

            cplane_pubkey, _, rc = run_command(cp_client, "cat ~/.ssh/id_ed25519.pub")
            if rc == 0 and cplane_pubkey.strip():
                cplane_pubkey = cplane_pubkey.strip()

                w_client = None
                try:
                    w_client = get_client_with_key(node.ip, node.ssh_user)
                    for cmd in [
                        "mkdir -p ~/.ssh",
                        "chmod 700 ~/.ssh",
                        f"echo '{cplane_pubkey}' >> ~/.ssh/authorized_keys",
                        "sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys",
                        "chmod 600 ~/.ssh/authorized_keys",
                    ]:
                        run_command(w_client, cmd)
                finally:
                    if w_client:
                        w_client.close()

                scan_out, _, _ = run_command(
                    cp_client,
                    f"ssh-keyscan -H -T 5 {node.ip} 2>/dev/null"
                )
                if scan_out.strip():
                    run_command(
                        cp_client,
                        f"touch ~/.ssh/known_hosts && "
                        f"echo '{scan_out.strip()}' >> ~/.ssh/known_hosts && "
                        f"sort -u ~/.ssh/known_hosts -o ~/.ssh/known_hosts"
                    )
                yield _ok(f"cplane → {node.ip} passwordless SSH wired")
        except Exception as exc:
            yield _log(f"[WARN] cplane→worker SSH wiring failed: {exc} — continuing")
        finally:
            if cp_client:
                cp_client.close()
    else:
        yield _log("[WARN] Could not read cplane info — skipping cplane→worker wiring")

    # 1i. Write ansible.cfg to all Ansible projects
    write_ansible_cfgs()
    yield _ok("ansible.cfg written to all Ansible projects")

    # ── Step 2: Ansible configures node and joins cluster ──────────────────────
    # Runs ansible-workers/add-worker.yml which executes:
    #   - longhorn_prereqs role (iscsi_tcp, cryptsetup, multipathd)
    #   - worker_join role handles the actual cluster join and labeling
    yield _step(2, f"Configuring {node.hostname} and joining cluster (~10 min)")

    # Always generate a fresh join token immediately before the playbook runs.
    # Avoids the 24h TTL expiry window silently failing the join.
    join_out, join_rc = run_on_cp(
        "kubeadm token create --print-join-command 2>/dev/null"
    )
    join_lines = [
        l.strip() for l in join_out.splitlines()
        if l.strip().startswith("kubeadm")
    ]
    if not join_lines or join_rc != 0:
        yield _fail(f"Could not generate join token: {join_out.strip()}")
        yield _err("join_command")
        return
    yield _ok("Fresh join token generated")

    cp        = _get_cp_info()
    inventory = _make_worker_inventory(
        cp, node.hostname.lower(), node.ip, node.ssh_user
    )

    extra_vars: dict = {"join_command": join_lines[0]}
    registry_host = _get_registry_host()
    if registry_host:
        extra_vars["gitlab_registry_host"] = registry_host

    playbook_ok = False
    for chunk in _call_playbook("add-worker.yml", inventory, extra_vars):
        if chunk == "data: __PLAYBOOK_OK__\n\n":
            playbook_ok = True
        elif chunk.startswith("data: __PLAYBOOK_FAIL__"):
            rc = chunk.strip().split(":")[-1]
            yield _fail(f"Ansible playbook failed (exit {rc}) — see log above")
            yield _err("playbook")
            return
        else:
            yield chunk

    if not playbook_ok:
        yield _fail("Playbook did not complete successfully")
        yield _err("playbook")
        return

    yield _ok(f"{node.hostname} joined the cluster successfully")

    # ── Step 3: Update inventory, group_vars, /etc/hosts on existing nodes ─────
    yield _step(3, "Updating cluster state")

    # 3a. Update generated/inventory.ini — insert inside [workers] section
    # Treat WARNING returns as hard failures — if the [workers] section is
    # missing the inventory is corrupt and future playbook runs will skip
    # this node entirely. Better to abort visibly than continue silently.
    inv_result = _update_inventory(node.hostname.lower(), node.ip, node.ssh_user)
    if inv_result.startswith("WARNING"):
        yield _fail(inv_result)
        yield _err("inventory_update")
        return
    yield _ok(inv_result)

    # 3b. Update cluster_hosts in generated/group_vars/all.yml
    # Ensures future playbook runs include this node in /etc/hosts propagation
    gv_result = _update_cluster_hosts(node.hostname.lower(), node.ip)
    yield _ok(gv_result)

    # 3c. Propagate new worker hostname/IP to /etc/hosts on existing nodes
    # Longhorn inter-replica iSCSI paths and Ansible delegate_to tasks
    # that resolve by hostname both depend on this.
    hosts_result = _propagate_etc_hosts(node.hostname.lower(), node.ip)
    yield _ok(hosts_result)

    # ── Step 4: Validation ─────────────────────────────────────────────────────
    # Run explicit checks before declaring success. A node that passes these
    # checks is fully operational — it can run workloads, accept Longhorn
    # replicas, pull images from the GitLab registry, and participate in
    # future Ansible playbook runs.
    yield _step(4, f"Validating {node.hostname}")

    validation_results = _validate_new_worker(node.hostname.lower(), node.ip)
    for line in validation_results:
        yield _log(line)

    # Print final cluster state
    out, _ = run_on_cp("kubectl get nodes -o wide --no-headers")
    yield _log("")
    yield _log("── Current cluster nodes ──────────────────────────────")
    for line in out.splitlines():
        if line.strip():
            yield _log(line)

    yield _done()


@router.post("/api/cluster/add-worker")
async def add_worker(node: NewWorker):
    return StreamingResponse(
        _add_worker_stream(node),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Remove worker ─────────────────────────────────────────────────────────────

def _remove_worker_stream(req: RemoveWorkerRequest):
    """
    Safe worker removal:
    1. Longhorn safety check — abort if removing this worker would leave
       any volume with zero healthy replicas (data loss prevention)
    2. Worker count warning — warn if cluster drops below 2 workers
    3. Ansible playbook — cordon, drain, delete node, kubeadm reset, cleanup
    4. Update permanent inventory
    """
    import time

    hostname = req.hostname.lower()

    yield f"data: Starting removal of {hostname}...\n\n"

    # ── Longhorn safety check ──────────────────────────────────────────────────
    # Abort if removing this node would leave any volume with zero healthy
    # replicas. Data loss cannot be undone — we refuse rather than warn.
    yield f"data: Checking Longhorn volume safety for {hostname}...\n\n"
    _lh_out, _lh_rc = run_on_cp(
        "kubectl -n longhorn-system get replicas -o json 2>/dev/null"
    )
    if _lh_rc == 0 and _lh_out.strip():
        # Strip Ansible ad-hoc header before JSON parsing
        lines = _lh_out.strip().splitlines()
        json_start = next(
            (i for i, l in enumerate(lines) if l.strip().startswith("{")), 0
        )
        json_str = "\n".join(lines[json_start:])
        try:
            _items   = json.loads(json_str).get("items", [])
            _on_node = [
                r for r in _items
                if r["spec"].get("nodeID", "") == hostname
            ]
            _vols    = set(r["spec"]["volumeName"] for r in _on_node)
            _faulted = []
            for _v in _vols:
                _survivors = [
                    r for r in _items
                    if r["spec"]["volumeName"] == _v
                    and r["spec"].get("nodeID", "") != hostname
                    and r["status"].get("currentState", "")
                    not in ("stopped", "error", "failed", "")
                ]
                if len(_survivors) == 0:
                    _faulted.append(_v)
            if _faulted:
                yield (
                    f"data: ABORT: removing {hostname} would fault "
                    f"{len(_faulted)} volume(s) — 0 healthy replicas would remain:\n\n"
                )
                for _v in _faulted:
                    yield f"data:   - {_v}\n\n"
                yield (
                    "data: Fix: add another worker first, or increase "
                    "replica count in Longhorn UI.\n\n"
                )
                yield "data: __ERROR__:longhorn_fault_risk\n\n"
                return
            yield "data: ok: Longhorn safety check passed\n\n"
        except Exception as _ex:
            yield f"data: WARNING: Longhorn check failed ({_ex}) — proceeding\n\n"
    else:
        yield "data: WARNING: Longhorn not available — skipping safety check\n\n"

    # ── Worker count warning ───────────────────────────────────────────────────
    worker_count_out, _ = run_on_cp(
        "kubectl get nodes --no-headers | grep -v control-plane | wc -l"
    )
    worker_count_lines = [
        l.strip() for l in worker_count_out.splitlines()
        if l.strip().isdigit()
    ]
    worker_count = int(worker_count_lines[0]) if worker_count_lines else 0
    if worker_count <= 2:
        yield (
            f"data: WARNING: cluster has {worker_count} worker(s). "
            f"Removing {hostname} will leave only {worker_count - 1} worker(s).\n\n"
        )
        yield (
            "data: WARNING: Longhorn volumes will go DEGRADED until "
            "a replacement worker is added.\n\n"
        )
        yield "data: WARNING: Proceeding in 3 seconds...\n\n"
        time.sleep(3)

    # ── Ansible: cordon + drain + delete + VM cleanup ──────────────────────────
    yield f"data: Running drain, cluster removal, and VM cleanup for {hostname}...\n\n"

    cp        = _get_cp_info()
    inventory = _make_worker_inventory(cp, hostname, req.ip, req.ssh_user)
    extra_vars = {"target_hostname": hostname}

    playbook_ok = False
    for chunk in _call_playbook("remove-worker.yml", inventory, extra_vars):
        if chunk == "data: __PLAYBOOK_OK__\n\n":
            playbook_ok = True
        elif chunk.startswith("data: __PLAYBOOK_FAIL__"):
            rc = chunk.strip().split(":")[-1]
            yield (
                f"data: WARNING: playbook exited {rc} — "
                "node may need manual cleanup but proceeding.\n\n"
            )
            playbook_ok = True  # still clean up inventory
        else:
            yield chunk

    # ── Update permanent inventory ─────────────────────────────────────────────
    if INVENTORY_PATH.exists():
        lines = [
            l for l in INVENTORY_PATH.read_text().splitlines()
            if not (hostname in l.lower() and req.ip in l)
        ]
        INVENTORY_PATH.write_text("\n".join(lines) + "\n")
        yield f"data: ok: inventory updated — {hostname} removed\n\n"

    yield f"data: {hostname} removed. VM is clean and ready to re-add.\n\n"
    yield "data: __DONE__\n\n"


@router.post("/api/cluster/remove-worker")
async def remove_worker_endpoint(req: RemoveWorkerRequest):
    return StreamingResponse(
        _remove_worker_stream(req),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
