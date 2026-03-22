"""
routes/deploy.py — Phases 4, 5, 6, 7

Phase 4: Stream ansible-k8s output via SSE
Phase 5: Validate Kubernetes cluster health
Phase 6: Stream ansible-longhorn output via SSE
Phase 7: Validate Longhorn storage health
"""
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from core.paths import (
    ANSIBLE_K8S_DIR,
    ANSIBLE_LONGHORN_DIR,
    INVENTORY_PATH,
    VARS_PATH,
    SSH_KEY_PATH,
)

router = APIRouter()


# ── Shared SSE stream helper ───────────────────────────────────────────────────

def _ansible_stream(playbook_dir: Path):
    """
    Generator that runs ansible-playbook and yields SSE-formatted lines.

    Yields:
        data: <ansible output line>\\n\\n   for each stdout line
        data: __DONE__\\n\\n                on success (rc=0)
        data: __ERROR__:<rc>\\n\\n          on failure
    """
    if not INVENTORY_PATH.exists():
        yield "data: __ERROR__:no_inventory\n\n"
        return

    cmd = [
        "ansible-playbook",
        "-i", str(INVENTORY_PATH),
        "site.yml",
        "--extra-vars", f"@{VARS_PATH}",
    ]

    process = subprocess.Popen(
        cmd,
        cwd=str(playbook_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    for line in iter(process.stdout.readline, ""):
        stripped = line.rstrip()
        if stripped:
            yield f"data: {stripped}\n\n"

    process.stdout.close()
    process.wait()

    if process.returncode == 0:
        yield "data: __DONE__\n\n"
    else:
        yield f"data: __ERROR__:{process.returncode}\n\n"


# ── Phase 4 — Deploy Kubernetes ────────────────────────────────────────────────

@router.get("/api/deploy/k8s/stream")
async def deploy_k8s_stream():
    """
    Stream ansible-k8s output line by line via SSE.
    Requires /api/configure to have been run first.
    """
    if not INVENTORY_PATH.exists():
        raise HTTPException(
            status_code=400,
            detail="No inventory found. Run POST /api/configure first."
        )
    return StreamingResponse(
        _ansible_stream(ANSIBLE_K8S_DIR),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# ── Phase 5 — Validate Kubernetes ──────────────────────────────────────────────

def _run_on_cp(cmd: str) -> tuple:
    """
    Run a shell command on the control plane via Ansible ad-hoc.
    Returns (stdout, returncode).
    """
    result = subprocess.run(
        [
            "ansible", "control_plane",
            "-i", str(INVENTORY_PATH),
            "-m", "shell",
            "-a", cmd,
            "--extra-vars", f"@{VARS_PATH}",
        ],
        capture_output=True, text=True
    )
    return result.stdout + result.stderr, result.returncode


@router.post("/api/validate/k8s")
async def validate_k8s():
    """
    Verify cluster health after ansible-k8s completes.
    Runs kubectl checks on the control plane via Ansible.
    """
    checks = {}

    # 1. All nodes Ready
    out, rc = _run_on_cp(
        "kubectl get nodes --no-headers | awk '{print $2}' | sort | uniq"
    )
    if rc != 0:
        checks["nodes_ready"] = f"FAIL — kubectl unreachable:\n{out}"
    elif "NotReady" in out:
        checks["nodes_ready"] = f"FAIL — some nodes NotReady:\n{out}"
    else:
        checks["nodes_ready"] = "ok"

    # 2. Node count > 0
    out, rc = _run_on_cp(
        "kubectl get nodes --no-headers | wc -l"
    )
    lines = [l.strip() for l in out.splitlines() if l.strip().isdigit()]
    count = int(lines[0]) if lines else 0
    checks["node_count"] = (
        f"ok ({count} nodes)" if count > 0
        else "FAIL — 0 nodes found"
    )

    # 3. CoreDNS running
    out, rc = _run_on_cp(
        "kubectl get pods -n kube-system -l k8s-app=kube-dns "
        "--no-headers | awk '{print $3}'"
    )
    checks["coredns"] = (
        "ok" if "Running" in out
        else f"FAIL — CoreDNS not Running:\n{out}"
    )

    # 4. kubeconfig artifact saved
    out, rc = _run_on_cp(
        "test -f ~/.kube/config && echo found || echo missing"
    )
    checks["kubeconfig"] = (
        "ok" if "found" in out
        else "FAIL — kubeconfig not found in cluster-artifacts"
    )

    failed = [k for k, v in checks.items() if v.startswith("FAIL")]
    return {
        "status": "error" if failed else "ok",
        "checks": checks
    }


# ── Join token ─────────────────────────────────────────────────────────────────

@router.get("/api/deploy/k8s/jointoken")
async def get_join_token():
    """
    Return the join command from cluster-artifacts on the control plane.
    Token TTL is 24 hours — show regeneration command if file missing.
    """
    out, rc = _run_on_cp(
        "cat ~/cluster-artifacts/join-command.txt 2>/dev/null "
        "|| echo __NOT_FOUND__"
    )

    if "__NOT_FOUND__" in out or rc != 0:
        return {
            "status": "not_found",
            "message": "Join command file not found on control plane.",
            "regenerate": "kubeadm token create --print-join-command",
            "ttl_note": "Tokens expire after 24h. CA hash never changes."
        }

    lines = [
        l.strip() for l in out.splitlines()
        if l.strip().startswith("kubeadm")
    ]
    join_cmd = lines[0] if lines else "not found"

    return {
        "status": "ok",
        "join_command": join_cmd,
        "location": "~/cluster-artifacts/join-command.txt",
        "ttl_note": "Token expires after 24h. To regenerate: "
                    "kubeadm token create --print-join-command"
    }


# ── Phase 6 — Deploy Longhorn ──────────────────────────────────────────────────

@router.get("/api/deploy/longhorn/stream")
async def deploy_longhorn_stream():
    """
    Stream ansible-longhorn output via SSE.
    Replica settings are already in generated/group_vars/all.yml.
    """
    if not INVENTORY_PATH.exists():
        raise HTTPException(
            status_code=400,
            detail="No inventory found. Run POST /api/configure first."
        )
    return StreamingResponse(
        _ansible_stream(ANSIBLE_LONGHORN_DIR),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# ── Phase 7 — Validate Longhorn ────────────────────────────────────────────────

@router.post("/api/validate/longhorn")
async def validate_longhorn():
    """
    Verify Longhorn health after ansible-longhorn completes.
    """
    checks = {}

    # 1. All Longhorn pods Running
    out, rc = _run_on_cp(
        "kubectl get pods -n longhorn-system --no-headers "
        "| awk '{print $3}' | sort | uniq -c"
    )
    if rc != 0:
        checks["pods"] = f"FAIL — kubectl error:\n{out}"
    elif "Running" in out and "Pending" not in out and "CrashLoop" not in out:
        checks["pods"] = f"ok — all Running"
    else:
        checks["pods"] = f"FAIL — unexpected pod states:\n{out}"

    # 2. Default StorageClass exists
    out, rc = _run_on_cp(
        "kubectl get storageclass --no-headers | grep '(default)'"
    )
    checks["storageclass"] = (
        f"ok — {out.strip()}" if "longhorn" in out
        else f"FAIL — no default Longhorn StorageClass:\n{out}"
    )

    # 3. Longhorn nodes Ready
    out, rc = _run_on_cp(
        "kubectl get nodes.longhorn.io -n longhorn-system "
        "--no-headers | awk '{print $2}' | sort | uniq -c"
    )
    checks["longhorn_nodes"] = (
        f"ok — {out.strip()}" if "True" in out and "False" not in out
        else f"FAIL — some Longhorn nodes not Ready:\n{out}"
    )

    # 4. Longhorn UI NodePort
    out, rc = _run_on_cp(
        "kubectl get svc longhorn-frontend -n longhorn-system "
        "--no-headers | awk '{print $5}'"
    )
    # extract NodePort from format like "80:31981/TCP"
    import re
    match = re.search(r":(\d+)/TCP", out)
    if match:
        port = match.group(1)
        cp_ip = VARS_PATH.read_text().split("cp_ip:")[1].split("\n")[0].strip().strip('"')
        checks["longhorn_ui"] = f"ok — http://{cp_ip}:{port}"
    else:
        checks["longhorn_ui"] = f"FAIL — could not get NodePort:\n{out}"

    failed = [k for k, v in checks.items() if v.startswith("FAIL")]
    return {
        "status": "error" if failed else "ok",
        "checks": checks
    }


# ── Add worker node ────────────────────────────────────────────────────────────

from pydantic import BaseModel as _BaseModel

class NewWorker(_BaseModel):
    ip: str
    hostname: str
    ssh_user: str
    ssh_pass: str


@router.post("/api/cluster/add-worker")
async def add_worker(node: NewWorker):
    """
    Add a new worker node to the running cluster.
    1. Push SSH key to new node (paramiko, password once)
    2. Fetch join command from control plane
    3. Run join command on new node
    4. Label node as worker
    5. Append node to inventory
    """
    from core.ssh import get_client_with_password, get_client_with_key, run_command
    from core.paths import SSH_PUB_KEY_PATH
    import socket
    import paramiko

    # Step 1 — push SSH key to new node
    pub_key = SSH_PUB_KEY_PATH.read_text().strip()
    client = None
    try:
        client = get_client_with_password(node.ip, node.ssh_user, node.ssh_pass)
        cmds = [
            "mkdir -p ~/.ssh",
            "chmod 700 ~/.ssh",
            f"echo '{pub_key}' >> ~/.ssh/authorized_keys",
            "sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys",
            "chmod 600 ~/.ssh/authorized_keys",
        ]
        for cmd in cmds:
            _, stderr, rc = run_command(client, cmd)
            if rc != 0:
                return {"status": "error", "step": "ssh_key",
                        "message": f"Command failed: {cmd} — {stderr}"}
    except paramiko.AuthenticationException:
        return {"status": "error", "step": "ssh_key",
                "message": "Authentication failed — wrong password?"}
    except (socket.timeout, paramiko.SSHException) as exc:
        return {"status": "error", "step": "ssh_key",
                "message": f"Connection failed: {exc}"}
    finally:
        if client:
            client.close()

    # Step 2 — fetch join command from control plane
    out, rc = _run_on_cp("cat ~/cluster-artifacts/join-command.txt")
    join_lines = [l.strip() for l in out.splitlines()
                  if l.strip().startswith("kubeadm")]
    if not join_lines:
        return {"status": "error", "step": "join_command",
                "message": "Could not read join command from control plane."}
    join_cmd = join_lines[0]

    # Step 3a — install kubeadm + kubelet on new node first
    prereq_cmds = [
        "sudo rm -f /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.asc /etc/apt/keyrings/docker.gpg 2>/dev/null; apt-get update -qq",
        "apt-get install -y apt-transport-https ca-certificates curl gpg",
        "mkdir -p /etc/apt/keyrings",
        "curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.30/deb/Release.key | gpg --batch --yes --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg",
        "echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.30/deb/ /' > /etc/apt/sources.list.d/kubernetes.list",
        "sudo rm -f /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.asc /etc/apt/keyrings/docker.gpg 2>/dev/null; apt-get update -qq",
        "apt-get install -y kubelet kubeadm",
        "systemctl enable kubelet",
    ]
    for cmd in prereq_cmds:
        prereq = subprocess.run(
            [
                "ansible", "-i", f"{node.ip},",
                "all", "-m", "shell",
                "-a", cmd,
                "-u", node.ssh_user,
                "--private-key", str(SSH_KEY_PATH),
                "--become",
            ],
            capture_output=True, text=True
        )
        if prereq.returncode != 0:
            return {"status": "error", "step": "prerequisites",
                    "message": f"Failed: {cmd}\n{prereq.stdout}\n{prereq.stderr}"}

    # Step 3b — install containerd
    containerd_cmds = [
        "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --batch --yes --dearmor -o /etc/apt/keyrings/docker.asc && chmod a+r /etc/apt/keyrings/docker.asc",
        "echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu jammy stable' > /etc/apt/sources.list.d/docker.list",
        "sudo rm -f /etc/apt/sources.list.d/docker.list /etc/apt/keyrings/docker.asc /etc/apt/keyrings/docker.gpg 2>/dev/null && apt-get update -qq && apt-get install -y containerd",
        "containerd config default > /etc/containerd/config.toml",
        "sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml",
        "systemctl restart containerd && systemctl enable containerd",
    ]
    for cmd in containerd_cmds:
        cr = subprocess.run(
            [
                "ansible", "-i", f"{node.ip},",
                "all", "-m", "shell",
                "-a", cmd,
                "-u", node.ssh_user,
                "--private-key", str(SSH_KEY_PATH),
                "--become",
            ],
            capture_output=True, text=True
        )
        if cr.returncode != 0:
            return {"status": "error", "step": "containerd",
                    "message": f"Failed: {cmd}\n{cr.stdout}\n{cr.stderr}"}

    # Step 3c — run join command on new node
    result = subprocess.run(
        [
            "ansible", "-i",
            f"{node.ip},",
            "all",
            "-m", "shell",
            "-a", f"sudo {join_cmd}",
            "-u", node.ssh_user,
            "--private-key", str(SSH_KEY_PATH),
            "--become",
        ],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return {"status": "error", "step": "join",
                "message": f"Join failed:\n{result.stdout}\n{result.stderr}"}

    # Step 4 — label node as worker
    _run_on_cp(
        f"kubectl label node {node.hostname} node-role.kubernetes.io/worker=worker --overwrite"
    )

    # Step 5 — append to inventory
    inv = INVENTORY_PATH.read_text()
    new_line = (
        f"{node.hostname} ansible_host={node.ip} "
        f"ansible_user={node.ssh_user}"
    )
    if new_line not in inv:
        inv = inv.replace(
            "[all:vars]",
            f"{new_line}\n\n[all:vars]"
        )
        INVENTORY_PATH.write_text(inv)

    # Step 6 — verify node joined
    out, _ = _run_on_cp("kubectl get nodes --no-headers")
    return {
        "status": "ok",
        "message": f"Node {node.hostname} joined successfully",
        "cluster_nodes": out
    }


# ── Reset ──────────────────────────────────────────────────────────────────────

class ResetRequest(_BaseModel):
    level: str          # "cluster" or "full"
    confirmation: str   # must equal "RESET"


def _reset_stream(level: str):
    """
    Generator that resets all cluster nodes via Ansible ad-hoc commands.
    Streams progress lines as SSE events.
    """
    if not INVENTORY_PATH.exists():
        yield "data: ERROR — no inventory found. Run configure first.\n\n"
        yield "data: __ERROR__:no_inventory\n\n"
        return

    yield "data: Starting reset...\n\n"

    # Commands to run on ALL nodes
    cluster_cmds = [
        ("kubeadm reset",
         "sudo kubeadm reset -f 2>/dev/null || true"),
        ("stop services",
         "sudo systemctl stop kubelet containerd 2>/dev/null || true"),
        ("remove packages",
         "sudo apt-get remove -y --allow-change-held-packages "
         "kubeadm kubelet kubectl containerd containerd.io 2>/dev/null || true"),
        ("autoremove",
         "sudo apt-get autoremove -y 2>/dev/null || true"),
        ("remove k8s dirs",
         "sudo rm -rf /etc/kubernetes /var/lib/etcd /var/lib/kubelet "
         "/var/lib/longhorn /etc/cni /opt/cni /var/lib/containerd"),
        ("remove apt sources",
         "sudo rm -f /etc/apt/sources.list.d/kubernetes.list "
         "/etc/apt/sources.list.d/docker.list "
         "/etc/apt/keyrings/kubernetes-apt-keyring.gpg "
         "/etc/apt/keyrings/docker.asc "
         "/etc/apt/keyrings/docker.gpg"),
        ("remove artifacts",
         "rm -rf ~/cluster-artifacts ~/.kube"),
        ("apt update",
         "sudo apt-get update -qq 2>/dev/null || true"),
    ]

    for label, cmd in cluster_cmds:
        yield f"data: [{label}] running on all nodes...\n\n"
        result = subprocess.run(
            [
                "ansible", "all",
                "-i", str(INVENTORY_PATH),
                "-m", "shell",
                "-a", cmd,
                "--become",
                "--extra-vars", f"@{VARS_PATH}",
            ],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            yield f"data: [{label}] done\n\n"
        else:
            yield f"data: [{label}] completed with warnings (non-fatal)\n\n"

    if level == "full":
        yield "data: [full wipe] removing SSH authorized keys from nodes...\n\n"
        result = subprocess.run(
            [
                "ansible", "all",
                "-i", str(INVENTORY_PATH),
                "-m", "shell",
                "-a", "rm -f ~/.ssh/authorized_keys",
                "--extra-vars", f"@{VARS_PATH}",
            ],
            capture_output=True, text=True
        )
        yield "data: [full wipe] clearing generated inventory...\n\n"
        INVENTORY_PATH.unlink(missing_ok=True)
        VARS_PATH.unlink(missing_ok=True)
        yield "data: [full wipe] done — machines are at absolute zero\n\n"

    yield "data: Reset complete. Nodes are clean and ready.\n\n"
    yield "data: __DONE__\n\n"


@router.get("/api/reset/stream")
async def reset_stream(level: str = "cluster", confirmation: str = ""):
    request = ResetRequest(level=level, confirmation=confirmation)
    """
    Reset cluster nodes. Requires confirmation == 'RESET'.
    Streams progress via SSE.
    """
    if request.confirmation != "RESET":
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=400,
            content={"error": "Type RESET in the confirmation field to proceed."}
        )
    if request.level not in ("cluster", "full"):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=400,
            content={"error": "level must be 'cluster' or 'full'"}
        )

    return StreamingResponse(
        _reset_stream(request.level),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
