"""
routes/longhorn.py — Phase 6 (Deploy Longhorn) and Phase 7 (Validate Longhorn).
"""
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from core.paths import ANSIBLE_LONGHORN_DIR, INVENTORY_PATH, VARS_PATH
from core.ansible import ansible_stream, run_on_cp

router = APIRouter()


@router.get("/api/deploy/longhorn/stream")
async def deploy_longhorn_stream():
    if not INVENTORY_PATH.exists():
        raise HTTPException(status_code=400, detail="No inventory found. Run POST /api/configure first.")
    return StreamingResponse(
        ansible_stream(ANSIBLE_LONGHORN_DIR),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/validate/longhorn")
async def validate_longhorn():
    checks = {}

    out, rc = run_on_cp("kubectl get pods -n longhorn-system --no-headers | awk '{print $3}' | sort | uniq -c")
    if rc != 0:
        checks["pods"] = f"FAIL — kubectl error:\n{out}"
    elif "Running" in out and "Pending" not in out and "CrashLoop" not in out:
        checks["pods"] = "ok — all Running"
    else:
        checks["pods"] = f"FAIL — unexpected pod states:\n{out}"

    out, rc = run_on_cp("kubectl get storageclass --no-headers | grep '(default)'")
    checks["storageclass"] = (
        f"ok — {out.strip()}" if "longhorn" in out
        else f"FAIL — no default Longhorn StorageClass:\n{out}"
    )

    out, rc = run_on_cp("kubectl get nodes.longhorn.io -n longhorn-system --no-headers | awk '{print $2}' | sort | uniq -c")
    checks["longhorn_nodes"] = (
        f"ok — {out.strip()}" if "True" in out and "False" not in out
        else f"FAIL — some Longhorn nodes not Ready:\n{out}"
    )

    out, rc = run_on_cp("kubectl get svc longhorn-frontend -n longhorn-system --no-headers | awk '{print $5}'")
    match = re.search(r":(\d+)/TCP", out)
    if match:
        port  = match.group(1)
        cp_ip = VARS_PATH.read_text().split("cp_ip:")[1].split("\n")[0].strip().strip('"')
        checks["longhorn_ui"] = f"ok — http://{cp_ip}:{port}"
    else:
        checks["longhorn_ui"] = f"FAIL — could not get NodePort:\n{out}"

    failed = [k for k, v in checks.items() if v.startswith("FAIL")]
    return {"status": "error" if failed else "ok", "checks": checks}
