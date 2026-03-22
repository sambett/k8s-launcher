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
    if ":" in out:
        port = out.strip().split(":")[1].split("/")[0]
        cp_ip = VARS_PATH.read_text().split('cp_ip:')[1].split('\n')[0].strip().strip('"')
        checks["longhorn_ui"] = f"ok — http://{cp_ip}:{port}"
    else:
        checks["longhorn_ui"] = f"FAIL — could not get NodePort:\n{out}"

    failed = [k for k, v in checks.items() if v.startswith("FAIL")]
    return {
        "status": "error" if failed else "ok",
        "checks": checks
    }
