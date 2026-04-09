"""
routes/worker.py — Add a new worker node to a running cluster.
Streams 7-step progress as SSE so the UI shows live feedback.
"""
import socket
import subprocess

import paramiko
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.paths import INVENTORY_PATH, VARS_PATH, SSH_KEY_PATH, SSH_PUB_KEY_PATH
from core.ssh import get_client_with_password, run_command
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
    return "1.7.22"  # fallback matches validated stack


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

    # ── Step 1 — SSH key push ─────────────────────────────────────────────────
    yield _step(1, f"Pushing SSH key to {node.ip}")

    pub_key = SSH_PUB_KEY_PATH.read_text().strip()
    client  = None
    try:
        client = get_client_with_password(node.ip, node.ssh_user, node.ssh_pass)
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

    # ── Step 1b — OS version gate (mirrors preflight check) ─────────────────────
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

    # ── Step 2 — Repair stale apt state ──────────────────────────────────────
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

    # ── Step 3 — Base packages + /etc/hosts ────────────────────────────
    yield _step(3, "Installing base packages and updating /etc/hosts")

    # Install socat and other base packages ansible-k8s common role provides
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

    # Populate /etc/hosts with cluster nodes from vars
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

    # ── Step 4 — Node prerequisites ──────────────────────────────────────────
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

    # ── Step 4 — containerd ───────────────────────────────────────────────────
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

    # ── Insecure registry (GitLab HTTP registry) ───────────────────────────────
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

    # ── Step 5 — Kubernetes packages ─────────────────────────────────────────
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

    # ── Step 6 — Join cluster ─────────────────────────────────────────────────
    yield _step(7, "Joining the cluster")

    # Always generate a fresh token — avoids 24h TTL expiry silently breaking joins
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

    # ── Step 7 — Label + inventory ────────────────────────────────────────────
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

class RemoveWorkerRequest(BaseModel):
    hostname: str
    ip:       str
    ssh_user: str
    ssh_pass: str  = ""   # required only for full reset
    mode:     str  = "soft"  # "soft" | "full"


def _remove_worker_stream(req: RemoveWorkerRequest):
    hostname = req.hostname.lower()
    STEPS    = 4 if req.mode == "full" else 3

    yield f"data: Starting {req.mode} removal of {hostname}...\n\n"

    # Step 1 — Cordon
    yield f"data: PLAY [Step 1/{STEPS}] Cordoning {hostname}...\n\n"
    out, rc = run_on_cp(f"kubectl cordon {hostname} 2>&1")
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"

    # FIX C: warn operator if removing this worker will leave only 1 worker.
    # With longhorn_replica_count=2, every volume drops to 1 replica (degraded)
    # until a replacement worker is added. Better to know before the drain runs.
    worker_count_out, _ = run_on_cp(
        "kubectl get nodes --no-headers "
        "| grep -v control-plane | wc -l"
    )
    worker_count_lines = [l.strip() for l in worker_count_out.splitlines() if l.strip().isdigit()]
    worker_count = int(worker_count_lines[0]) if worker_count_lines else 0
    if worker_count <= 2:
        yield f"data: ⚠ WARNING: cluster has {worker_count} worker(s). Removing {hostname} will leave only {worker_count - 1} worker(s).\n\n"
        yield f"data: ⚠ Longhorn volumes will go DEGRADED (1 replica instead of 2) until a replacement worker is added.\n\n"
        yield f"data: ⚠ Proceeding with removal in 3 seconds...\n\n"
        import time
        time.sleep(3)

    # Step 2 — Drain
    yield f"data: PLAY [Step 2/{STEPS}] Draining all pods from {hostname}...\n\n"
    out, rc = run_on_cp(
        f"kubectl drain {hostname} "
        f"--ignore-daemonsets --delete-emptydir-data --force --timeout=300s 2>&1"
    )
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"

    # Step 3 — Delete from cluster + update inventory
    yield f"data: PLAY [Step 3/{STEPS}] Removing {hostname} from cluster...\n\n"
    out, rc = run_on_cp(f"kubectl delete node {hostname} 2>&1")
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"

    # FIX B: remove Longhorn ghost node entry after K8s node deletion.
    # Without this Longhorn keeps the node in its registry and tries to
    # schedule replicas to a node that no longer exists, leaving volumes degraded.
    # --ignore-not-found makes this safe even if the node was never in Longhorn.
    run_on_cp(f"kubectl delete node.longhorn.io {hostname} -n longhorn-system --ignore-not-found 2>&1")
    yield f"data: ok: Longhorn node entry removed\n\n"

    if INVENTORY_PATH.exists():
        lines = [
            l for l in INVENTORY_PATH.read_text().splitlines()
            if not (hostname in l.lower() and req.ip in l)
        ]
        INVENTORY_PATH.write_text("\n".join(lines) + "\n")
        yield f"data: ok: inventory updated — {hostname} removed\n\n"

    if req.mode == "soft":
        yield f"data: {hostname} removed from cluster. VM is still running.\n\n"
        yield "data: Re-add it any time via the Add worker form above.\n\n"
        yield "data: __DONE__\n\n"
        return

    # Step 4 — Full reset on VM
    yield f"data: PLAY [Step 4/{STEPS}] Running full reset on {req.ip}...\n\n"
    if not req.ssh_pass:
        yield "data: ERROR: SSH password required for full reset.\n\n"
        yield "data: __ERROR__:no_ssh_pass\n\n"
        return

    client = None
    try:
        client = get_client_with_password(req.ip, req.ssh_user, req.ssh_pass)
        reset_steps = [
            ("kubeadm reset",
             "sudo kubeadm reset -f 2>/dev/null || true"),
            ("stop services",
             "sudo systemctl stop kubelet containerd 2>/dev/null || true"),
            ("remove packages",
             "sudo apt-get remove -y --allow-change-held-packages "
             "kubeadm kubelet kubectl containerd.io 2>/dev/null || true"),
            ("clean directories",
             "sudo rm -rf /etc/kubernetes /var/lib/etcd /var/lib/kubelet "
             "/etc/cni /opt/cni /var/lib/containerd /etc/containerd"),
            ("remove apt sources",
             "sudo rm -f /etc/apt/sources.list.d/kubernetes.list "
             "/etc/apt/sources.list.d/docker.list "
             "/etc/apt/keyrings/kubernetes-apt-keyring.gpg "
             "/etc/apt/keyrings/docker.gpg /etc/apt/keyrings/docker.asc"),
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
