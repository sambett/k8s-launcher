import os
import subprocess
import socket
import concurrent.futures
import tempfile
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn
import paramiko

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR             = Path(__file__).parent
GENERATED_DIR        = BASE_DIR / "generated"
TEMPLATES_DIR        = BASE_DIR / "templates"
ANSIBLE_K8S_DIR      = BASE_DIR / "ansible-k8s"
ANSIBLE_LONGHORN_DIR = BASE_DIR / "ansible-longhorn"
COMPAT_MATRIX_PATH   = BASE_DIR / "compat_matrix.json"

SSH_KEY_PATH     = Path.home() / ".ssh" / "id_ed25519"
SSH_PUB_KEY_PATH = Path.home() / ".ssh" / "id_ed25519.pub"

GENERATED_DIR.mkdir(parents=True, exist_ok=True)
(GENERATED_DIR / "group_vars").mkdir(parents=True, exist_ok=True)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="k8s-launcher")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── Models ─────────────────────────────────────────────────────────────────────
class NodeEntry(BaseModel):
    ip: str
    hostname: str
    ssh_user: str
    ssh_pass: str


class BootstrapSSHRequest(BaseModel):
    nodes: List[NodeEntry]


class PreflightRequest(BaseModel):
    nodes: List[dict]


class WorkerNode(BaseModel):
    ip: str
    hostname: str
    ssh_user: str


class ControlPlane(BaseModel):
    ip: str
    hostname: str
    ssh_user: str


class ClusterConfig(BaseModel):
    control_plane: ControlPlane
    workers: List[WorkerNode]
    kubernetes_version: str = "1.30.5"
    containerd_version: str = "1.7.22"
    calico_version: str = "3.28.2"
    longhorn_version: str = "1.7.2"
    pod_cidr: str = "192.168.0.0/16"
    service_cidr: str = "10.96.0.0/12"
    cluster_name: str = "k8s-cluster"
    deploy_longhorn: bool = True


# ── Dashboard ──────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Status ─────────────────────────────────────────────────────────────────────
@app.get("/api/status")
async def status():
    return {"status": "ok", "launcher": "k8s-launcher"}


# ── Phase 0 — Install Ansible ──────────────────────────────────────────────────
@app.post("/api/bootstrap/ansible")
async def install_ansible():
    import shutil
    # check if ansible binary exists on PATH
    if shutil.which("ansible"):
        check = subprocess.run(["ansible", "--version"],
                               capture_output=True, text=True)
        version_line = check.stdout.splitlines()[0]
        return {"status": "ok", "message": f"Already installed: {version_line}"}

    # not found — install via pip3
    install = subprocess.run(
        ["pip3", "install", "--user", "ansible"],
        capture_output=True, text=True
    )
    if install.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Ansible install failed:\n{install.stderr}"
        )

    # add ~/.local/bin to PATH for this process
    import os
    local_bin = str(Path.home() / ".local" / "bin")
    os.environ["PATH"] = local_bin + ":" + os.environ.get("PATH", "")

    verify = subprocess.run(["ansible", "--version"],
                            capture_output=True, text=True)
    version_line = verify.stdout.splitlines()[0] if verify.returncode == 0 else "unknown"
    return {"status": "ok", "message": f"Installed: {version_line}"}


# ── Phase 1 — SSH Key Bootstrap ────────────────────────────────────────────────
def _ensure_ssh_key() -> str:
    """
    Generate an ed25519 key pair on the controller if one does not exist.
    Returns the public key content.
    """
    if not SSH_KEY_PATH.exists():
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519",
             "-f", str(SSH_KEY_PATH), "-N", ""],
            check=True, capture_output=True
        )
    return SSH_PUB_KEY_PATH.read_text().strip()


def _push_key_to_node(ip: str, ssh_user: str, ssh_pass: str, pub_key: str) -> dict:
    """
    Connect with password once, append public key to authorized_keys.
    Password is in local scope only — discarded when function returns.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            hostname=ip,
            username=ssh_user,
            password=ssh_pass,
            timeout=15,
            look_for_keys=False,
            allow_agent=False
        )

        commands = [
            "mkdir -p ~/.ssh",
            "chmod 700 ~/.ssh",
            f"echo '{pub_key}' >> ~/.ssh/authorized_keys",
            "sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys",
            "chmod 600 ~/.ssh/authorized_keys",
        ]

        for cmd in commands:
            _, stdout, stderr = client.exec_command(cmd)
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                err = stderr.read().decode().strip()
                return {
                    "ip": ip,
                    "status": "error",
                    "message": f"Command failed: {cmd} — {err}"
                }

        return {"ip": ip, "status": "ok", "message": "SSH key pushed"}

    except paramiko.AuthenticationException:
        return {"ip": ip, "status": "error",
                "message": "Authentication failed — wrong password?"}
    except (socket.timeout, paramiko.SSHException) as exc:
        return {"ip": ip, "status": "error",
                "message": f"Connection failed: {exc}"}
    finally:
        client.close()


@app.post("/api/bootstrap/ssh")
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


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("launcher:app", host="0.0.0.0", port=5000, reload=True)
