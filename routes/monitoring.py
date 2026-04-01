"""
routes/monitoring.py — Prometheus + Grafana via kube-prometheus-stack Helm chart.
"""
import json
import re
from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from core.ansible import run_on_cp
from core.paths import COMPAT_MATRIX_PATH, INVENTORY_PATH, VARS_PATH

router = APIRouter()


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
    return {"versions": entries, "recommended": _match_version(entries, k8s_ver), "k8s_version": k8s_ver}


@router.get("/api/monitoring/access")
async def monitoring_access():
    cp_ip = _read_cp_ip()
    grafana_url = prometheus_url = None
    out, _ = run_on_cp(
        "kubectl get svc -n monitoring --no-headers 2>/dev/null | grep -i grafana | awk '{print $5}'"
    )
    m = re.search(r":(\d+)/TCP", out)
    if m and cp_ip:
        grafana_url = f"http://{cp_ip}:{m.group(1)}"
    out2, _ = run_on_cp(
        "kubectl get svc -n monitoring --no-headers 2>/dev/null "
        "| grep -i 'prometheus-operated\\|prometheus-kube' | head -1 | awk '{print $5}'"
    )
    m2 = re.search(r":(\d+)/TCP", out2)
    if m2 and cp_ip:
        prometheus_url = f"http://{cp_ip}:{m2.group(1)}"
    return {"grafana_url": grafana_url, "prometheus_url": prometheus_url}


def _monitoring_install_stream(version: str):
    if not INVENTORY_PATH.exists():
        yield "data: __ERROR__:no_inventory — run Configure first\n\n"
        return
    yield f"data: Installing kube-prometheus-stack {version}...\n\n"
    run_on_cp("kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f - 2>&1")
    yield "data: Namespace monitoring ready\n\n"
    yield "data: Adding prometheus-community helm repo...\n\n"
    out, _ = run_on_cp(
        "helm repo add prometheus-community "
        "https://prometheus-community.github.io/helm-charts && helm repo update 2>&1"
    )
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"
    yield f"data: Deploying chart {version} (3-5 minutes)...\n\n"
    out, rc = run_on_cp(
        f"helm upgrade --install kube-prometheus-stack "
        f"prometheus-community/kube-prometheus-stack "
        f"--namespace monitoring --version {version} "
        f"--set grafana.service.type=NodePort "
        f"--set grafana.service.nodePort=32300 "
        f"--set prometheus.prometheusSpec.service.type=NodePort "
        f"--set prometheus.prometheusSpec.service.nodePort=32301 "
        f"--timeout 10m --wait 2>&1"
    )
    for line in out.splitlines():
        if line.strip():
            yield f"data: {line}\n\n"
    if rc != 0:
        yield f"data: __ERROR__:{rc}\n\n"
        return
    yield "data: ✓ Grafana on NodePort 32300 — login: admin / prom-operator\n\n"
    yield "data: ✓ Prometheus on NodePort 32301\n\n"
    yield "data: __DONE__\n\n"


@router.get("/api/monitoring/install/stream")
async def monitoring_install_stream(version: str = ""):
    if not version:
        return JSONResponse(status_code=400, content={"error": "version required"})
    return StreamingResponse(
        _monitoring_install_stream(version),
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
