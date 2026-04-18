"""
routes/worker.py — Add and remove worker nodes.
Streams progress as SSE so the UI shows live feedback.

Add-worker flow:
  Step 1 (Python/Paramiko) — Full SSH trust setup using the password once:
    1. Push controller public key → new worker authorized_keys
    2. Write passwordless sudo on new worker
    3. Verify sudo works without password
    4. ssh-keyscan → populate controller known_hosts for new worker
    5. Wire cplane → new worker (generate cplane keypair if missing,
       push cplane pubkey to new worker, populate cplane known_hosts)
    6. write_ansible_cfgs() → write ansible.cfg with StrictHostKeyChecking=no
  Step 2 (Ansible) — Full node config via ansible-workers/add-worker.yml
    Installs containerd, Kubernetes packages, joins the cluster, labels node.
  Step 3 (Python) — Update permanent inventory

  After Step 1, the new worker is fully trusted by both ansiblectl and cplane.
  The password is used exactly once and never stored.

Remove-worker flow:
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

# Shared helper — writes ansible.cfg with StrictHostKeyChecking=no to every
# Ansible project directory. Called at the end of Step 1 so it is guaranteed
# to be in place before the add-worker Ansible playbook runs in Step 2.
from core.ansible_cfg import write_ansible_cfgs

router = APIRouter()


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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_registry_host() -> str:
    """Read the GitLab registry host from generated jupyterhub vars."""
    from core.paths import JUPYTERHUB_VARS_PATH
    try:
        for line in JUPYTERHUB_VARS_PATH.read_text().splitlines():
            if line.strip().startswith("jhub_registry_host:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _get_cp_info() -> dict:
    """
    Read control plane IP and SSH user from generated files.
    IP comes from group_vars/all.yml, SSH user comes from inventory.ini.
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


def _make_inventory(cp: dict, worker_hostname: str,
                    worker_ip: str, worker_user: str) -> str:
    """
    Build a minimal inventory string for a single-worker ansible-workers run.
    The control plane entry is needed so delegate_to tasks can reach kubectl.
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


# ── Add worker ────────────────────────────────────────────────────────────────

def _add_worker_stream(node: NewWorker):
    """
    Three-step worker onboarding streamed live to the browser.
    """
    TOTAL = 3

    def _step(n, msg): return f"data: PLAY [Step {n}/{TOTAL}] {msg}\n\n"
    def _ok(msg):      return f"data: ok: {msg}\n\n"
    def _fail(msg):    return f"data: FAILED: {msg}\n\n"
    def _log(msg):     return f"data: {msg}\n\n"
    def _done():       return "data: __DONE__\n\n"
    def _err(code):    return f"data: __ERROR__:{code}\n\n"

    # ── Step 1: Full SSH trust setup ──────────────────────────────────────────
    # Uses the password once via Paramiko. After this step the new worker is
    # fully trusted by both ansiblectl (controller) and cplane, and Ansible
    # can run against it with zero interactive prompts.
    yield _step(1, f"Bootstrapping SSH key and sudo on {node.ip}")

    pub_key = SSH_PUB_KEY_PATH.read_text().strip()
    client  = None
    try:
        client = get_client_with_password(node.ip, node.ssh_user, node.ssh_pass)

        # 1a. Push controller public key
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

        # 1b. Write passwordless sudo
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

        # 1c. Verify sudo works without password before closing session
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

    # 1d. Populate controller known_hosts for the new worker
    # Done after closing the Paramiko session — uses local ssh-keyscan binary.
    # This is the programmatic equivalent of typing 'yes' at the fingerprint prompt.
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
            parts = line.split()
            key_blob = parts[-1] if len(parts) >= 3 else ""
            if key_blob and key_blob not in existing:
                with open(known_hosts_path, "a") as f:
                    f.write(line + "\n")
    yield _ok(f"Controller known_hosts updated for {node.ip}")

    # 1e. Wire cplane → new worker passwordless SSH
    # The controller already has key-based access to cplane (bootstrap pushed it).
    # We SSH into cplane, generate its keypair if missing, read its pubkey,
    # push it to the new worker, and populate cplane's known_hosts.
    # Non-fatal — cluster join still works if this fails, but cplane operations
    # targeting the new worker would prompt without it.
    cp = _get_cp_info()
    if cp["ip"] and cp["user"]:
        cp_client = None
        try:
            cp_client = get_client_with_key(cp["ip"], cp["user"], str(SSH_KEY_PATH))

            # Generate cplane keypair if it doesn't exist yet
            _, _, rc = run_command(cp_client, "test -f ~/.ssh/id_ed25519.pub")
            if rc != 0:
                run_command(
                    cp_client,
                    "ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N '' -q"
                )

            # Read cplane's public key
            cplane_pubkey, _, rc = run_command(cp_client, "cat ~/.ssh/id_ed25519.pub")
            if rc == 0 and cplane_pubkey.strip():
                cplane_pubkey = cplane_pubkey.strip()

                # Push cplane pubkey to new worker using controller key
                w_client = None
                try:
                    w_client = get_client_with_key(
                        node.ip, node.ssh_user, str(SSH_KEY_PATH)
                    )
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

                # Populate cplane known_hosts for the new worker
                # ssh-keyscan runs ON cplane so cplane's own known_hosts is updated
                scan_out, _, _ = run_command(
                    cp_client, f"ssh-keyscan -H -T 5 {node.ip} 2>/dev/null"
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
            # Non-fatal — log warning and continue to the Ansible step
            yield _log(f"[WARN] cplane→worker SSH wiring failed: {exc} — continuing")
        finally:
            if cp_client:
                cp_client.close()
    else:
        yield _log("[WARN] Could not read cplane info — skipping cplane→worker wiring")

    # 1f. Write ansible.cfg to all Ansible projects
    # Always overwrites — guarantees StrictHostKeyChecking=no is in place
    # before Step 2 runs the add-worker playbook against this new node.
    write_ansible_cfgs()
    yield _ok("ansible.cfg written to all Ansible projects")

    # ── Step 2: Ansible configures node and joins cluster ─────────────────────
    # Runs ansible-workers/add-worker.yml which installs containerd, Kubernetes
    # packages, joins the cluster via kubeadm, and labels the node as a worker.
    # All connection trust is already established by Step 1 — no prompts possible.
    yield _step(2, f"Configuring {node.hostname} and joining cluster (~8 min)")

    # Always generate a fresh join token — avoids 24h TTL expiry silently failing
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

    cp = _get_cp_info()
    inventory = _make_inventory(cp, node.hostname.lower(), node.ip, node.ssh_user)

    extra_vars: dict = {"join_command": join_lines[0]}
    registry_host = _get_registry_host()
    if registry_host:
        # Pass GitLab registry host so the new worker can pull notebook images
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

    # ── Step 3: Update permanent inventory ───────────────────────────────────
    # Add the new worker to generated/inventory.ini so future playbook runs
    # (Longhorn, JupyterHub, monitoring) automatically include it.
    yield _step(3, "Updating inventory")

    new_line = f"{node.hostname} ansible_host={node.ip} ansible_user={node.ssh_user}"
    if INVENTORY_PATH.exists():
        inv = INVENTORY_PATH.read_text()
        if new_line not in inv:
            inv = inv.replace("[all:vars]", f"{new_line}\n\n[all:vars]")
            INVENTORY_PATH.write_text(inv)
            yield _ok(f"Inventory updated with {node.hostname}")
        else:
            yield _ok(f"{node.hostname} already in inventory — no change")

    # Print current cluster state for confirmation
    out, _ = run_on_cp("kubectl get nodes -o wide --no-headers")
    yield _log("")
    yield _log("── Current cluster nodes ─────────────────────────")
    for line in out.splitlines():
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

    # ── Longhorn safety check ─────────────────────────────────────────────────
    # Check if any volume has ALL its replicas on this node.
    # If yes, removing it would cause data loss — abort and explain how to fix.
    yield f"data: Checking Longhorn volume safety for {hostname}...\n\n"
    _lh_out, _lh_rc = run_on_cp(
        "kubectl -n longhorn-system get replicas -o json 2>/dev/null"
    )
    if _lh_rc == 0 and _lh_out.strip().startswith("{"):
        try:
            _items = json.loads(_lh_out).get("items", [])
            _on_node = [
                r for r in _items if r["spec"].get("nodeID", "") == hostname
            ]
            _vols = set(r["spec"]["volumeName"] for r in _on_node)
            _faulted = []
            for _v in _vols:
                _survivors = [
                    r for r in _items
                    if r["spec"]["volumeName"] == _v
                    and r["spec"].get("nodeID", "") != hostname
                    and r["status"].get("currentState", "")
                    not in ("stopped", "error", "")
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
                    "data: Fix: add another worker first, or raise "
                    "replica count in Longhorn UI.\n\n"
                )
                yield "data: __ERROR__:longhorn_fault_risk\n\n"
                return
            yield "data: ok: Longhorn safety check passed\n\n"
        except Exception as _ex:
            yield f"data: WARNING: Longhorn check failed ({_ex}) — proceeding\n\n"
    else:
        yield "data: WARNING: Longhorn not available — skipping safety check\n\n"

    # ── Worker count warning ──────────────────────────────────────────────────
    worker_count_out, _ = run_on_cp(
        "kubectl get nodes --no-headers | grep -v control-plane | wc -l"
    )
    worker_count_lines = [
        l.strip() for l in worker_count_out.splitlines() if l.strip().isdigit()
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

    # ── Ansible: cordon + drain + delete + cleanup ────────────────────────────
    yield f"data: Running drain, cluster removal, and VM cleanup for {hostname}...\n\n"

    cp = _get_cp_info()
    inventory  = _make_inventory(cp, hostname, req.ip, req.ssh_user)
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

    # ── Update permanent inventory ────────────────────────────────────────────
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
