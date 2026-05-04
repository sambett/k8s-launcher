"""
routes/monitoring.py — Prometheus + Grafana via kube-prometheus-stack Helm chart.
"""
import json
import re
from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from core.ansible import ansible_stream, run_on_cp, read_cp_ip
from core.paths import (
    ANSIBLE_MONITORING_DIR,
    COMPAT_MATRIX_PATH,
    INVENTORY_PATH,
    VARS_PATH,
)

router = APIRouter()
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _strip_header(out: str) -> str:
    """
    Remove the Ansible ad-hoc header line from run_on_cp output.

    run_on_cp() returns stdout + stderr from an ansible ad-hoc shell command.
    Ansible always prepends a header line before the real output:
        ansiblecplane | SUCCESS | rc=0 >>
        <actual command output>

    The header is identified by ' | rc=' which is unique to it.
    Everything after that line is the real output.
    """
    lines = out.strip().splitlines()
    for i, line in enumerate(lines):
        if " | " in line and " | rc=" in line:
            return "\n".join(lines[i + 1:]).strip()
    return out.strip()


# ── Status ─────────────────────────────────────────────────────────────────────

@router.get("/api/monitoring/status")
async def monitoring_status():
    """
    Check whether kube-prometheus-stack is installed via Helm.
    Returns installed status and the chart version string if found.
    """
    out, rc = run_on_cp(
        "helm list -n monitoring --no-headers 2>/dev/null "
        "| grep kube-prometheus-stack"
    )
    clean = _strip_header(out)
    if rc != 0 or not clean:
        return {"status": "not_installed", "chart": ""}
    # helm list columns: name  namespace  revision  updated  status  chart  app_version
    # index:              0     1          2         3        4       5      6
    parts = clean.split()
    chart = parts[5] if len(parts) > 5 else "unknown"
    return {"status": "installed", "chart": chart}


# ── Versions ───────────────────────────────────────────────────────────────────

@router.get("/api/monitoring/versions")
async def monitoring_versions():
    """
    Return kube-prometheus-stack versions from compat_matrix.json.
    Marks the recommended version based on the current cluster k8s minor.
    """
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


# ── Access URLs ────────────────────────────────────────────────────────────────

@router.get("/api/monitoring/access")
async def monitoring_access():
    """
    Return live Grafana and Prometheus NodePort URLs.
    Reads the CP IP from the generated vars file via core.ansible.read_cp_ip().
    Scrapes the actual NodePort from the running services so the URL is always
    correct even if the port was overridden during install.
    """
    cp_ip = read_cp_ip()
    grafana_url = prometheus_url = None

    out, _ = run_on_cp(
        "kubectl get svc -n monitoring --no-headers 2>/dev/null "
        "| grep 'kube-prometheus-stack-grafana ' | awk '{print $5}'"
    )
    m = re.search(r":(\d+)/TCP", _strip_header(out))
    if m and cp_ip:
        grafana_url = f"http://{cp_ip}:{m.group(1)}"

    out2, _ = run_on_cp(
        "kubectl get svc kube-prometheus-stack-prometheus "
        "-n monitoring --no-headers 2>/dev/null | awk '{print $5}'"
    )
    m2 = re.search(r":(\d+)/TCP", _strip_header(out2))
    if m2 and cp_ip:
        prometheus_url = f"http://{cp_ip}:{m2.group(1)}"

    return {"grafana_url": grafana_url, "prometheus_url": prometheus_url}


# ── Install stream ─────────────────────────────────────────────────────────────

@router.get("/api/monitoring/install/stream")
async def monitoring_install_stream(version: str = ""):
    """
    SSE stream: install or upgrade kube-prometheus-stack via ansible-monitoring.
    version must be a valid X.Y.Z chart version string.
    """
    if not version:
        return JSONResponse(
            status_code=400,
            content={"error": "version required"}
        )
    if not VERSION_RE.match(version):
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid version '{version}' — expected X.Y.Z"}
        )
    if not INVENTORY_PATH.exists():
        return JSONResponse(
            status_code=400,
            content={"error": "No inventory — run Configure first"}
        )
    return StreamingResponse(
        ansible_stream(ANSIBLE_MONITORING_DIR, extra_vars={"chart_version": version}),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Private helpers ────────────────────────────────────────────────────────────

def _read_k8s_version() -> str:
    """Read kubernetes_version from the generated group_vars file."""
    if VARS_PATH.exists():
        for line in VARS_PATH.read_text().splitlines():
            if line.strip().startswith("kubernetes_version:"):
                return line.split(":", 1)[1].strip().strip('"')
    return ""


def _match_version(entries: list, k8s_version: str):
    """
    Return the first chart version whose k8s_min/k8s_max range covers
    the running cluster's minor version. Returns None if no match.
    """
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
