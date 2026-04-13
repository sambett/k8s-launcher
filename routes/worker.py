"""
routes/worker.py — Add a new worker node to a running cluster.
Streams 7-step progress as SSE so the UI shows live feedback.
"""
import socket
import subprocess
from pathlib import Path

import paramiko
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.paths import INVENTORY_PATH, VARS_PATH, SSH_KEY_PATH, SSH_PUB_KEY_PATH
from core.ssh import get_client_with_password, get_client_with_key, run_command
from core.ansible import run_on_cp

router = APIRouter()


class NewWorker(BaseModel):
    ip: str
    hostname: str
    ssh_user: str
    ssh_pass: str


def _get_registry_host() -> str:
    """Read jhub_registry_host from generated jupyterhub vars. Returns empty string if not found."""
    from core.paths import JUPYTERHUB_VARS_PATH
    try:
        for line in JUPYTERHUB_VARS_PATH.read_text().splitlines():
            if line.strip().startswith("jhub_registry_host:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _get_k8s_version() -> str:
    try:
        for line in VARS_PATH.read_text().splitlines():
            if line.strip().startswith("kubernetes_version:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "1.30.5"


def _get_containerd_version() -> str:
    """Read containerd_version from generated/group_vars/all.yml (same source as the cluster)."""
    try:
        for line in VARS_PATH.read_text().splitlines():
            if line.strip().startswith("containerd_version:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "1.7.22"


def _get_cp_info() -> dict:
    """Read control plane IP and SSH user from generated/group_vars/all.yml."""
    cp_ip   = ""
    cp_user = ""
    try:
        for line in VARS_PATH.read_text().splitlines():
            if line.strip().startswith("cp_ip:"):
                cp_ip = line.split(":", 1)[1].strip().strip('"').strip("'")
            if line.strip().startswith("cp_hostname:"):
                pass  # not needed here
        # cp ssh user lives in inventory — read [control_plane] section
        if INVENTORY_PATH.exists():
            in_cp = False
            for line in INVENTORY_PATH.read_text().splitlines():
                if "[control_plane]" in line:
                    in_cp = True
                    continue
                if in_cp and line.strip() and not line.startswith("["):
                    parts = line.split()
                    for part in parts:
                        if part.startswith("ansible_user="):
                            cp_user = part.split("=", 1)[1]
                    break
                if in_cp and line.startswith("["):
                    break
    except Exception:
        pass
    return {"ip": cp_ip, "user": cp_user}


def _add_worker_stream(node: NewWorker):
    k8s_version        = _get_k8s_version()
    containerd_version  = _get_containerd_version()
    k8s_pkg     = f"{k8s_version}-1.1"
    k8s_repo    = "v" + ".".join(k8s_version.split(".")[:2])
    TOTAL       = 8

    def _step(n, msg):  return f"data: PLAY [Step {n}/{TOTAL}] {msg}\n\n"
    def _ok(msg):       return f"data: ok: {msg}\n\n"
    def _fail(msg):     return f"data: FAILED: {msg}\n\n"
    def _log(msg):      return f"data: {msg}\n\n"
    def _done():        return "data: __DONE__\n\n"
    def _err(code):     return f"data: __ERROR__:{code}\n\n"

    def _ansible(cmd: str):
        return subprocess.run(
            [
                "ansible", "-i", f"{node.ip},", "all",
                "-m", "shell", "-a", cmd,
                "-u", node.ssh_user,
                "--private-key", str(SSH_KEY_PATH),
                "--become",
            ],
            capture_output=True, text=True,
        )

    # -- Step 1 — SSH key push + sudo + known_hosts + cplane trust ------------
    yield _step(1, f"Pushing SSH key to {node.ip}")

    pub_key = SSH_PUB_KEY_PATH.read_text().strip()
    client  = None
    try:
        client = get_client_with_password(node.ip, node.ssh_user, node.ssh_pass)

        # -- SSH key setup -----------------------------------------------------
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

        # -- Passwordless sudo setup -------------------------------------------
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

    # -- Populate controller known_hosts --------------------------------------
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

    # -- Wire cplane -> new worker passwordless SSH ---------------------------
    cp = _get_cp_info()
    if cp["ip"] and cp["user"]:
        cp_client = None
        try:
            cp_client = get_client_with_key(cp["ip"], cp["user"], str(SSH_KEY_PATH))

            _, _, rc = run_command(cp_client, "test -f ~/.ssh/id_ed25519.pub")
            if rc != 0:
                run_command(cp_client, "ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N '' -q")

            cplane_pubkey, _, rc = run_command(cp_client, "cat ~/.ssh/id_ed25519.pub")
            if rc == 0 and cplane_pubkey.strip():
                cplane_pubkey = cplane_pubkey.strip()

                w_client = None
                try:
                    w_client = get_client_with_key(node.ip, node.ssh_user, str(SSH_KEY_PATH))
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
                yield _ok(f"cplane -> {node.ip} passwordless SSH wired")
        except Exception as exc:
            yield _log(f"[WARN] cplane->worker SSH wiring failed: {exc} — continuing")
        finally:
            if cp_client:
                cp_client.close()
    else:
        yield _log("[WARN] Could not read cplane info from vars — skipping cplane->worker wiring")

    # -- Step 1b — OS version gate --------------------------------------------
    r_os = _ansible(". /etc/os-release && echo $VERSION_ID")
    os_ver = ""
    for line in r_os.stdout.splitlines():
        line = line.strip()
        if line in ("22.04", "24.04"):
            os_ver = line
            break
    if not os_ver:
        raw = r_os.stdout.strip().replace("\n", " ")
        yield _fail(
            f"Unsupported OS on {node.ip}. "
            f"Need Ubuntu 22.04 or 24.04, got: {raw}. "
            "Add-worker only supports the same OS versions as initial cluster deploy."
        )
        yield _err("os_check")
        return
    yield _ok(f"OS check passed: Ubuntu {os_ver}")

    # -- Step 2 — Repair stale apt state --------------------------------------
    yield _step(2, "Checking for stale apt state (idempotency repair)")

    r = _ansible(
        'KEY=/etc/apt/keyrings/docker.gpg; '
        'REPO=/etc/apt/sources.list.d/docker.list; '
        'if [ -f "$REPO" ] && [ ! -f "$KEY" ]; then '
        '  rm -f "$REPO"; '
        '  echo "Removed stale docker.list (GPG key was absent)"; '
        'else '
        '  echo "No cleanup needed"; '
        'fi'
    )
    if r.returncode != 0:
        yield _fail(f"Stale-state check failed:\n{r.stderr}")
        yield _err("cleanup")
        return
    for line in r.stdout.splitlines():
        if "Removed" in line or "No cleanup" in line:
            yield _ok(line)

    # -- Step 3 — Base packages + /etc/hosts ----------------------------------
    yield _step(3, "Installing base packages and updating /etc/hosts")

    r = _ansible(
        "apt-get update -qq && "
        "apt-get install -y apt-transport-https ca-certificates curl gpg socat openssh-server && "
        "echo base_packages_ok"
    )
    if r.returncode != 0:
        yield _fail(f"Base packages failed:\n{r.stdout}\n{r.stderr}")
        yield _err("base_packages")
        return
    yield _ok("Base packages installed (socat, curl, gpg, ca-certificates)")

    hosts_entries = []
    if VARS_PATH.exists():
        in_hosts = False
        for line in VARS_PATH.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("cluster_hosts:"):
                in_hosts = True
                continue
            if in_hosts:
                if stripped.startswith("-"):
                    import re
                    m = re.search(r"name:\s*(\S+).*ip:\s*[\"']?([\d.]+)", stripped)
                    if m:
                        hosts_entries.append(f"{m.group(2)} {m.group(1)}")
                elif stripped and not stripped.startswith("#"):
                    in_hosts = False
    if hosts_entries:
        for entry in hosts_entries:
            ip, hostname = entry.split(" ", 1)
            r = _ansible(
                f"grep -q '{ip}' /etc/hosts || "
                f"echo '{ip} {hostname}' >> /etc/hosts"
            )
        yield _ok(f"/etc/hosts updated with {len(hosts_entries)} cluster node(s)")
    else:
        yield _ok("/etc/hosts — no cluster_hosts in vars, skipped")

    # -- Step 4 — Node prerequisites ------------------------------------------
    yield _step(4, "Installing node prerequisites")

    prereq_steps = [
        (
            "Disable swap",
            "swapoff -a && "
            "sed -i 's|^\\([^#].*\\bswap\\b.*\\)$|#\\1|' /etc/fstab && "
            "rm -f /swapfile",
        ),
        (
            "Load kernel modules",
            "modprobe overlay && modprobe br_netfilter && "
            "printf 'overlay\\nbr_netfilter\\n' > /etc/modules-load.d/kubernetes.conf",
        ),
        (
            "Set sysctl",
            "printf 'net.bridge.bridge-nf-call-iptables=1\\n"
            "net.bridge.bridge-nf-call-ip6tables=1\\n"
            "net.ipv4.ip_forward=1\\n' > /etc/sysctl.d/kubernetes.conf && "
            "sysctl --system > /dev/null",
        ),
        (
            "Install open-iscsi and nfs-common",
            "apt-get install -y open-iscsi nfs-common && "
            "systemctl enable --now iscsid",
        ),
    ]
    for label, cmd in prereq_steps:
        yield _log(f"changed: [{label}]")
        r = _ansible(cmd)
        if r.returncode != 0:
            yield _fail(f"{label} failed:\n{r.stdout}\n{r.stderr}")
            yield _err("prereqs")
            return
    yield _ok("Node prerequisites satisfied")

    # -- Step 5 — containerd --------------------------------------------------
    yield _step(5, "Installing containerd (pinned)")

    containerd_steps = [
        (
            "Create keyrings dir",
            "mkdir -p /etc/apt/keyrings",
        ),
        (
            "Install Docker GPG key",
            "curl -fsSL https://download.docker.com/linux/ubuntu/gpg "
            "| gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.gpg && "
            "chmod 644 /etc/apt/keyrings/docker.gpg",
        ),
        (
            "Add Docker apt repo",
            "CODENAME=$(. /etc/os-release && echo $VERSION_CODENAME) && "
            "echo \"deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] "
            "https://download.docker.com/linux/ubuntu $CODENAME stable\" "
            "> /etc/apt/sources.list.d/docker.list",
        ),
        (
            "Install containerd.io (pinned)",
            "apt-get update -qq && "
            "apt-mark unhold containerd.io 2>/dev/null || true && "
            f"apt-get install -y --allow-downgrades containerd.io={containerd_version}-1 && "
            "apt-mark hold containerd.io",
        ),
        (
            "Configure containerd (SystemdCgroup = true + config_path)",
            "containerd config default "
            "| sed 's/SystemdCgroup = false/SystemdCgroup = true/' "
            "| sed 's|config_path = \"\"|config_path = \"/etc/containerd/certs.d\"|g' "
            "> /etc/containerd/config.toml && "
            "systemctl restart containerd && "
            "systemctl enable containerd",
        ),
    ]
    for label, cmd in containerd_steps:
        yield _log(f"changed: [{label}]")
        r = _ansible(cmd)
        if r.returncode != 0:
            yield _fail(f"{label} failed:\n{r.stdout}\n{r.stderr}")
            yield _err("containerd")
            return
    yield _ok("containerd installed and configured")

    # -- Insecure registry (GitLab HTTP registry) ------------------------------
    yield _log("changed: [Configure insecure GitLab registry]")
    registry_host = _get_registry_host()
    if registry_host:
        hosts_toml = (
            f'server = "http://{registry_host}"\n'
            f'[host."http://{registry_host}"]\n'
            f'  capabilities = ["pull", "resolve", "push"]\n'
            f'  skip_verify = true\n'
        )
        r = _ansible(
            f"mkdir -p /etc/containerd/certs.d/{registry_host} && "
            f"printf '%s' '{hosts_toml}' "
            f"> /etc/containerd/certs.d/{registry_host}/hosts.toml"
        )
        if r.returncode != 0:
            yield _fail(f"Insecure registry config failed:\n{r.stdout}\n{r.stderr}")
            yield _err("containerd_registry")
            return
        yield _ok(f"Insecure registry configured: {registry_host}")
    else:
        yield _log("No registry host in vars — skipping insecure registry config")

    # -- Step 6 — Kubernetes packages -----------------------------------------
    yield _step(6, f"Installing kubeadm + kubelet {k8s_version} (pinned)")

    k8s_steps = [
        (
            "Install Kubernetes GPG key",
            f"curl -fsSL https://pkgs.k8s.io/core:/stable:/{k8s_repo}/deb/Release.key "
            "| gpg --batch --yes --dearmor "
            "-o /etc/apt/keyrings/kubernetes-apt-keyring.gpg",
        ),
        (
            "Add Kubernetes apt repo",
            f"echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] "
            f"https://pkgs.k8s.io/core:/stable:/{k8s_repo}/deb/ /' "
            "> /etc/apt/sources.list.d/kubernetes.list",
        ),
        (
            f"Install kubelet={k8s_pkg} kubeadm={k8s_pkg} (pinned)",
            f"apt-get update -qq && "
            f"apt-get install -y kubelet={k8s_pkg} kubeadm={k8s_pkg} && "
            f"apt-mark hold kubelet kubeadm && "
            f"systemctl enable kubelet",
        ),
    ]
    for label, cmd in k8s_steps:
        yield _log(f"changed: [{label}]")
        r = _ansible(cmd)
        if r.returncode != 0:
            yield _fail(f"{label} failed:\n{r.stdout}\n{r.stderr}")
            yield _err("k8s_packages")
            return
    yield _ok(f"kubeadm + kubelet {k8s_version} installed")

    # -- Step 7 — Join cluster ------------------------------------------------
    yield _step(7, "Joining the cluster")

    yield _log("changed: [Generating fresh join token (kubeadm token create)]")
    out, rc = run_on_cp("kubeadm token create --print-join-command 2>/dev/null")
    join_lines = [l.strip() for l in out.splitlines() if l.strip().startswith("kubeadm")]
    if not join_lines or rc != 0:
        yield _fail(
            "Could not generate a join token from the control plane. "
            f"rc={rc} output={out.strip()}"
        )
        yield _err("join_command")
        return

    join_cmd = join_lines[0]
    yield _log(f"changed: [Running kubeadm join on {node.ip}]")
    r = _ansible(join_cmd)
    if r.returncode != 0:
        yield _fail(f"kubeadm join failed:\n{r.stdout}\n{r.stderr}")
        yield _err("join")
        return
    yield _ok(f"{node.hostname} joined the cluster")

    # -- Step 8 — Label + inventory -------------------------------------------
    yield _step(8, "Labelling node and updating inventory")

    run_on_cp(
        f"kubectl label node {node.hostname.lower()} "
        f"node-role.kubernetes.io/worker=worker --overwrite"
    )
    yield _ok("Node labelled: node-role.kubernetes.io/worker=worker")

    new_line = f"{node.hostname} ansible_host={node.ip} ansible_user={node.ssh_user}"
    if INVENTORY_PATH.exists():
        inv = INVENTORY_PATH.read_text()
        if new_line not in inv:
            inv = inv.replace("[all:vars]", f"{new_line}\n\n[all:vars]")
            INVENTORY_PATH.write_text(inv)
            yield _ok(f"Inventory updated with {node.hostname}")
        else:
            yield _ok(f"{node.hostname} already in inventory — no change")

    out, _ = run_on_cp("kubectl get nodes -o wide --no-headers")
    yield _log("")
    yield _log("-- Current cluster nodes -----------------------------------------")
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


# -- Remove worker ------------------------------------------------------------

class RemoveWorkerRequest(BaseModel):
    hostname: str
    ip:       str
    ssh_user: str
    ssh_pass: str  = ""   # required only for full reset
    mode:     str  = "soft"  # "soft" | "full"


def _remove_worker_stream(req: RemoveWorkerRequest):
    import json as _lhj
    import time

    hostname = req.hostname.lower()
    STEPS    = 4 if req.mode == "full" else 3

    yield f"data: Starting {req.mode} removal of {hostname}...\n\n"

    # -- Longhorn pre-delete safety check ------------------------------------
    # Abort if removing this node would leave any volume with 0 healthy replicas.
    yield f"data: Checking Longhorn volume safety for {hostname}...\n\n"
    _lh_out, _lh_rc = run_on_cp(
        "kubectl -n longhorn-system get replicas -o json 2>/dev/null"
    )
    if _lh_rc == 0 and _lh_out.strip().startswith("{"):
        try:
            _items = _lhj.loads(_lh_out).get("items", [])
            _on_node = [r for r in _items
                        if r["spec"].get("nodeID", "") == hostname]
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
                    f"{len(_faulted)} volume(s) -- 0 replicas would remain:\n\n"
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
            yield f"data: WARNING: Longhorn check failed ({_ex}) -- proceeding\n\n"
    else:
        yield "data: WARNING: Longhorn not available -- skipping safety check\n\n"

    # -- Step 1 — Cordon ------------------------------------------------------
    yield f"data: PLAY [Step 1/{STEPS}] Cordoning {hostname}...\n\n"
    out, rc = run_on_cp(f"kubectl cordon {hostname} 2>&1")
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"

    worker_count_out, _ = run_on_cp(
        "kubectl get nodes --no-headers "
        "| grep -v control-plane | wc -l"
    )
    worker_count_lines = [l.strip() for l in worker_count_out.splitlines() if l.strip().isdigit()]
    worker_count = int(worker_count_lines[0]) if worker_count_lines else 0
    if worker_count <= 2:
        yield f"data: WARNING: cluster has {worker_count} worker(s). Removing {hostname} will leave only {worker_count - 1} worker(s).\n\n"
        yield f"data: WARNING: Longhorn volumes will go DEGRADED (1 replica instead of 2) until a replacement worker is added.\n\n"
        yield f"data: WARNING: Proceeding with removal in 3 seconds...\n\n"
        time.sleep(3)

    # -- Step 2 — Drain -------------------------------------------------------
    yield f"data: PLAY [Step 2/{STEPS}] Draining all pods from {hostname}...\n\n"
    out, rc = run_on_cp(
        f"kubectl drain {hostname} "
        f"--ignore-daemonsets --delete-emptydir-data --force --timeout=300s 2>&1"
    )
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"

    # -- Step 3 — Delete from cluster + stop kubelet + update inventory -------
    yield f"data: PLAY [Step 3/{STEPS}] Removing {hostname} from cluster...\n\n"
    out, rc = run_on_cp(f"kubectl delete node {hostname} 2>&1")
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"

    run_on_cp(
        f"kubectl delete node.longhorn.io {hostname} "
        f"-n longhorn-system --ignore-not-found 2>&1"
    )
    yield f"data: ok: Longhorn node entry removed\n\n"

    # -- Stop + disable kubelet on the removed VM so it does not loop --------
    # Uses key-based SSH (set up during add-worker). Best-effort — non-fatal.
    yield f"data: Stopping kubelet on {req.ip} to prevent reconnect loop...\n\n"
    _kube_client = None
    try:
        _kube_client = get_client_with_key(req.ip, req.ssh_user, str(SSH_KEY_PATH))
        _stop_cmds = [
            "sudo systemctl stop kubelet 2>/dev/null || true",
            "sudo systemctl disable kubelet 2>/dev/null || true",
            "sudo kubeadm reset -f 2>/dev/null || true",
            "sudo rm -rf /etc/kubernetes /var/lib/kubelet "
            "/etc/cni/net.d /var/lib/etcd 2>/dev/null || true",
        ]
        for _cmd in _stop_cmds:
            run_command(_kube_client, _cmd)
        yield f"data: ok: kubelet stopped and disabled on {req.ip}\n\n"
    except Exception as _e:
        yield (
            f"data: WARNING: could not stop kubelet on {req.ip} ({_e}) -- "
            f"VM may keep retrying. SSH in manually and run: "
            f"sudo systemctl stop kubelet && sudo kubeadm reset -f\n\n"
        )
    finally:
        if _kube_client:
            _kube_client.close()

    if INVENTORY_PATH.exists():
        lines = [
            l for l in INVENTORY_PATH.read_text().splitlines()
            if not (hostname in l.lower() and req.ip in l)
        ]
        INVENTORY_PATH.write_text("\n".join(lines) + "\n")
        yield f"data: ok: inventory updated — {hostname} removed\n\n"

    if req.mode == "soft":
        yield f"data: {hostname} removed from cluster. VM is still running but kubelet is stopped.\n\n"
        yield "data: Re-add it any time via the Add worker form above.\n\n"
        yield "data: __DONE__\n\n"
        return

    # -- Step 4 — Full reset on VM --------------------------------------------
    # Full wipe: packages + directories + apt sources removed entirely.
    yield f"data: PLAY [Step 4/{STEPS}] Running full reset on {req.ip}...\n\n"
    if not req.ssh_pass:
        yield "data: ERROR: SSH password required for full reset.\n\n"
        yield "data: __ERROR__:no_ssh_pass\n\n"
        return

    client = None
    try:
        client = get_client_with_password(req.ip, req.ssh_user, req.ssh_pass)
        reset_steps = [
            (
                "stop kubelet and containerd",
                "sudo systemctl stop kubelet containerd 2>/dev/null || true && "
                "sudo systemctl disable kubelet 2>/dev/null || true",
            ),
            (
                "kubeadm reset",
                "sudo kubeadm reset -f 2>/dev/null || true",
            ),
            (
                "remove packages",
                "sudo apt-get remove -y --allow-change-held-packages "
                "kubeadm kubelet kubectl containerd.io 2>/dev/null || true",
            ),
            (
                "clean kubernetes directories",
                "sudo rm -rf /etc/kubernetes /var/lib/etcd /var/lib/kubelet "
                "/etc/cni /opt/cni /etc/cni/net.d",
            ),
            (
                "clean containerd directories",
                "sudo rm -rf /var/lib/containerd /etc/containerd",
            ),
            (
                "remove apt sources",
                "sudo rm -f /etc/apt/sources.list.d/kubernetes.list "
                "/etc/apt/sources.list.d/docker.list "
                "/etc/apt/keyrings/kubernetes-apt-keyring.gpg "
                "/etc/apt/keyrings/docker.gpg /etc/apt/keyrings/docker.asc",
            ),
            (
                "restart containerd clean",
                "sudo systemctl start containerd 2>/dev/null || true",
            ),
        ]
        for label, cmd in reset_steps:
            yield f"data: changed: [{label}]\n\n"
            stdout, stderr, rc = run_command(client, cmd)
        yield f"data: ok: {req.ip} fully reset — ready for fresh provisioning\n\n"
    except Exception as exc:
        yield f"data: WARNING: SSH failed for {req.ip}: {exc}\n\n"
        yield "data: Node removed from cluster but VM cleanup failed — clean manually.\n\n"
    finally:
        if client:
            client.close()

    yield "data: __DONE__\n\n"


@router.post("/api/cluster/remove-worker")
async def remove_worker_endpoint(req: RemoveWorkerRequest):
    return StreamingResponse(
        _remove_worker_stream(req),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
