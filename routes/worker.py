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


def _get_k8s_version() -> str:
    try:
        for line in VARS_PATH.read_text().splitlines():
            if line.strip().startswith("kubernetes_version:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "1.30.5"


def _add_worker_stream(node: NewWorker):
    k8s_version = _get_k8s_version()
    k8s_pkg     = f"{k8s_version}-1.1"
    k8s_repo    = "v" + ".".join(k8s_version.split(".")[:2])
    TOTAL       = 7

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

    # ── Step 3 — Node prerequisites ──────────────────────────────────────────
    yield _step(3, "Installing node prerequisites")

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
    yield _step(4, "Installing containerd (pinned)")

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
            "echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] "
            "https://download.docker.com/linux/ubuntu jammy stable' "
            "> /etc/apt/sources.list.d/docker.list",
        ),
        (
            "Install containerd.io (pinned)",
            "apt-get update -qq && "
            "apt-get install -y containerd.io && "
            "apt-mark hold containerd.io",
        ),
        (
            "Configure containerd (SystemdCgroup = true)",
            "containerd config default "
            "| sed 's/SystemdCgroup = false/SystemdCgroup = true/' "
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

    # ── Step 5 — Kubernetes packages ─────────────────────────────────────────
    yield _step(5, f"Installing kubeadm + kubelet {k8s_version} (pinned)")

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
    yield _step(6, "Joining the cluster")

    out, rc = run_on_cp("cat ~/cluster-artifacts/join-command.txt 2>/dev/null")
    join_lines = [l.strip() for l in out.splitlines() if l.strip().startswith("kubeadm")]
    if not join_lines:
        yield _fail(
            "Could not read join-command.txt from the control plane. "
            "The token may have expired (24h TTL). "
            "Regenerate it: kubeadm token create --print-join-command"
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
    yield _step(7, "Labelling node and updating inventory")

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
