"""
routes/monitoring.py — Prometheus + Grafana via kube-prometheus-stack Helm chart.
"""
import json
import re
from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from core.ansible import ansible_stream, run_on_cp
from core.paths import (
    ANSIBLE_MONITORING_DIR,
    COMPAT_MATRIX_PATH,
    INVENTORY_PATH,
    VARS_PATH,
)

router = APIRouter()
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

@router.get("/api/monitoring/status")
async def monitoring_status():
    out, rc = run_on_cp(
        "helm list -n monitoring --no-headers 2>/dev/null | grep kube-prometheus-stack"
    )
    if rc != 0 or not out.strip():
        return {"status": "not_installed"}
    parts = out.strip().split()
    return {"status": "installed", "chart": parts[8] if len(parts) > 8 else "unknown"}

@router.get("/api/monitoring/versions")
async def monitoring_versions():
    if not COMPAT_MATRIX_PATH.exists():
        return {"versions": [], "recommended": None, "k8s_version": ""}
    matrix  = json.loads(COMPAT_MATRIX_PATH.read_text())
    entries = matrix.get("kube_prometheus_stack", [])
    k8s_ver = _read_k8s_version()
    return {
        "versions":    entries,
        "recommended": _match_version(entries, k8s_ver),
        "k8s_version": k8s_ver,
    }

@router.get("/api/monitoring/access")
async def monitoring_access():
    cp_ip = _read_cp_ip()
    grafana_url = prometheus_url = None

    out, _ = run_on_cp(
        "kubectl get svc -n monitoring --no-headers 2>/dev/null "
        "| grep 'kube-prometheus-stack-grafana ' | awk '{print $5}'"
    )
    m = re.search(r":(\d+)/TCP", out)
    if m and cp_ip:
        grafana_url = f"http://{cp_ip}:{m.group(1)}"

    out2, _ = run_on_cp(
        "kubectl get svc kube-prometheus-stack-prometheus "
        "-n monitoring --no-headers 2>/dev/null | awk '{print $5}'"
    )
    m2 = re.search(r":(\d+)/TCP", out2)
    if m2 and cp_ip:
        prometheus_url = f"http://{cp_ip}:{m2.group(1)}"

    return {"grafana_url": grafana_url, "prometheus_url": prometheus_url}

@router.get("/api/monitoring/install/stream")
async def monitoring_install_stream(version: str = ""):
    if not version:
        return JSONResponse(status_code=400, content={"error": "version required"})
    if not VERSION_RE.match(version):
        return JSONResponse(status_code=400, content={"error": f"Invalid version '{version}' — expected X.Y.Z"})
    if not INVENTORY_PATH.exists():
        return JSONResponse(status_code=400, content={"error": "No inventory — run Configure first"})
    return StreamingResponse(
        ansible_stream(ANSIBLE_MONITORING_DIR, extra_vars={"chart_version": version}),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

def _read_k8s_version() -> str:
    if VARS_PATH.exists():
        for line in VARS_PATH.read_text().splitlines():
            if line.strip().startswith("kubernetes_version:"):
                return line.split(":", 1)[1].strip().strip('"')
    return ""

def _read_cp_ip() -> str:
    if VARS_PATH.exists():
        for line in VARS_PATH.read_text().splitlines():
            if line.strip().startswith("cp_ip:"):
                return line.split(":", 1)[1].strip().strip('"')
    return ""

def _match_version(entries: list, k8s_version: str):
    if not k8s_version or not entries:
        return None
    try:
        minor = int(k8s_version.split(".")[1])
        for entry in entries:
            lo = int(entry.get("k8s_min", "0").split(".")[1])
            hi = int(entry.get("k8s_max", "99").split(".")[1])
            if lo <= minor <= hi:
                return entry["version"]
    except (ValueError, IndexError):
        pass
    return None
