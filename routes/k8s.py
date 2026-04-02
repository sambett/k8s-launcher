"""
routes/k8s.py — Phase 4 (Deploy Kubernetes) and Phase 5 (Validate Kubernetes).
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from core.paths import ANSIBLE_K8S_DIR, INVENTORY_PATH
from core.ansible import ansible_stream, run_on_cp

router = APIRouter()


@router.get("/api/deploy/k8s/stream")
async def deploy_k8s_stream():
    if not INVENTORY_PATH.exists():
        raise HTTPException(status_code=400, detail="No inventory found. Run POST /api/configure first.")
    return StreamingResponse(
        ansible_stream(ANSIBLE_K8S_DIR),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/validate/k8s")
async def validate_k8s():
    checks = {}

    out, rc = run_on_cp("kubectl get nodes --no-headers | awk '{print $2}' | sort | uniq")
    if rc != 0:
        checks["nodes_ready"] = f"FAIL — kubectl unreachable:\n{out}"
    elif "NotReady" in out:
        checks["nodes_ready"] = f"FAIL — some nodes NotReady:\n{out}"
    else:
        checks["nodes_ready"] = "ok"

    out, rc = run_on_cp("kubectl get nodes --no-headers | wc -l")
    lines = [l.strip() for l in out.splitlines() if l.strip().isdigit()]
    count = int(lines[0]) if lines else 0
    checks["node_count"] = f"ok ({count} nodes)" if count > 0 else "FAIL — 0 nodes found"

    out, rc = run_on_cp("kubectl get pods -n kube-system -l k8s-app=kube-dns --no-headers | awk '{print $3}'")
    checks["coredns"] = "ok" if "Running" in out else f"FAIL — CoreDNS not Running:\n{out}"

    out, rc = run_on_cp("test -f ~/.kube/config && echo found || echo missing")
    checks["kubeconfig"] = "ok" if "found" in out else "FAIL — kubeconfig not found"

    failed = [k for k, v in checks.items() if v.startswith("FAIL")]
    return {"status": "error" if failed else "ok", "checks": checks}


@router.get("/api/deploy/k8s/jointoken")
async def get_join_token():
    # Always generate a fresh token so the card never shows an expired command
    out, rc = run_on_cp("kubeadm token create --print-join-command 2>/dev/null")
    lines = [l.strip() for l in out.splitlines() if l.strip().startswith("kubeadm")]
    if not lines or rc != 0:
        return {
            "status": "error",
            "message": "Could not generate join token — is the control plane reachable?",
            "detail": out.strip()
        }
    return {
        "status": "ok",
        "join_command": lines[0],
        "ttl_note": "Fresh token — valid for 24h from now."
    }
