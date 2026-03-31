import concurrent.futures
from typing import List

import paramiko
from fastapi import APIRouter
from pydantic import BaseModel

from core.ssh import get_client_with_key, run_command

router = APIRouter()


# ── Models ─────────────────────────────────────────────────────────────────────

class PreflightRequest(BaseModel):
    nodes: List[dict]   # [{"ip": "...", "hostname": "...", "ssh_user": "..."}]


# ── Phase 2 — Preflight checks ─────────────────────────────────────────────────

def _check_node(ip: str, ssh_user: str, hostname: str) -> dict:
    """
    Connect via key-based SSH and run 7 readiness checks.
    Returns a result dict with per-check outcomes.
    """
    checks = {}
    client = None

    try:
        client = get_client_with_key(ip, ssh_user)
    except Exception as exc:
        return {
            "hostname": hostname,
            "ip": ip,
            "status": "error",
            "checks": {"ssh": f"FAIL — {exc}"}
        }

    def run(cmd):
        out, _, _ = run_command(client, cmd)
        return out

    # SSH
    checks["ssh"] = "ok"

    # OS version
    out = run("lsb_release -rs")
    checks["os"] = (
        f"ok ({out})" if out in ("22.04", "24.04")
        else f"FAIL — got {out}, need 22.04 or 24.04"
    )

    # RAM
    out = run("free -m | awk '/^Mem:/{print $2}'")
    try:
        ram_mb = int(out)
        checks["ram"] = (
            f"ok ({ram_mb} MB)" if ram_mb >= 1800
            else f"FAIL — {ram_mb} MB, need >= 1800 MB"
        )
    except ValueError:
        checks["ram"] = f"FAIL — could not parse: {out}"

    # Disk
    out = run("df -m / | awk 'NR==2{print $4}'")
    try:
        disk_mb = int(out)
        checks["disk"] = (
            f"ok ({disk_mb} MB free)" if disk_mb >= 20000
            else f"FAIL — {disk_mb} MB free, need >= 20000 MB"
        )
    except ValueError:
        checks["disk"] = f"FAIL — could not parse: {out}"

    # Swap
    out = run("swapon --show")
    checks["swap"] = "ok (off)" if out == "" else f"FAIL — swap is on: {out}"

    # Python 3
    out = run("python3 --version")
    checks["python3"] = (
        f"ok ({out})" if out.startswith("Python 3")
        else f"FAIL — {out or 'not found'}"
    )

    # kubeadm state
    out = run(
        "test -f /etc/kubernetes/admin.conf && echo exists || echo absent"
    )
    checks["kubeadm_state"] = (
        "ok (clean)" if out == "absent"
        else "FAIL — /etc/kubernetes/admin.conf exists, previous cluster found"
    )

    if client:
        client.close()

    failed = [k for k, v in checks.items() if v.startswith("FAIL")]
    return {
        "hostname": hostname,
        "ip": ip,
        "status": "error" if failed else "ok",
        "checks": checks
    }


@router.post("/api/preflight")
async def preflight(request: PreflightRequest):
    """
    Run all node checks in parallel.
    Returns sorted results with per-node per-check detail.
    """
    results = []
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = {
            executor.submit(
                _check_node,
                node["ip"],
                node["ssh_user"],
                node["hostname"]
            ): node
            for node in request.nodes
        }
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    failed = [r for r in results if r["status"] == "error"]
    return {
        "status": "error" if failed else "ok",
        "results": sorted(results, key=lambda r: r["hostname"])
    }
