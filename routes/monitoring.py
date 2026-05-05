"""
routes/monitoring.py — Prometheus + Grafana via kube-prometheus-stack Helm chart.
                        DCGM Exporter DaemonSet for GPU metrics.
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

# kube-prometheus-stack chart versions: X.Y.Z
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# DCGM image tags: 3.3.5-3.4.0-ubuntu22.04
DCGM_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+-\d+\.\d+\.\d+-ubuntu\d+\.\d+$")


def _strip_header(out: str) -> str:
    """
    Remove the Ansible ad-hoc header line from run_on_cp output.

    Ansible always prepends a header before the real output:
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


# ── Stack status ───────────────────────────────────────────────────────────────

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


# ── Stack versions ─────────────────────────────────────────────────────────────

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
    Reads CP IP from generated vars file via core.ansible.read_cp_ip().
    Scrapes the actual NodePort from the running services so the URL is
    always correct even if the port was overridden during install.
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


# ── Stack install stream ───────────────────────────────────────────────────────

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
        ansible_stream(
            ANSIBLE_MONITORING_DIR,
            extra_vars={"chart_version": version, "deploy_dcgm": "false"}
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── DCGM status ────────────────────────────────────────────────────────────────

@router.get("/api/monitoring/dcgm/status")
async def dcgm_status():
    """
    Check whether the DCGM Exporter DaemonSet is running.
    Returns running pod count and desired pod count so the UI can
    show a meaningful status (e.g. '1/1 ready' on a single GPU node).
    """
    out, rc = run_on_cp(
        "kubectl get daemonset dcgm-exporter -n monitoring "
        "-o jsonpath='{.status.numberReady}/{.status.desiredNumberScheduled}' "
        "2>/dev/null"
    )
    clean = _strip_header(out)
    if rc != 0 or not clean or "/" not in clean:
        return {"status": "not_installed", "ready": 0, "desired": 0}
    try:
        ready, desired = clean.split("/")
        ready, desired = int(ready), int(desired)
    except ValueError:
        return {"status": "not_installed", "ready": 0, "desired": 0}
    if desired == 0:
        return {"status": "not_installed", "ready": 0, "desired": 0}
    status = "ready" if ready == desired else "degraded"
    return {"status": status, "ready": ready, "desired": desired}


# ── DCGM versions ──────────────────────────────────────────────────────────────

@router.get("/api/monitoring/dcgm/versions")
async def dcgm_versions():
    """
    Return DCGM Exporter image versions from compat_matrix.json.
    No k8s version filtering — DCGM compatibility is driver-based not k8s-based.
    The recommended flag is set directly in the matrix entry.
    """
    if not COMPAT_MATRIX_PATH.exists():
        return {"versions": [], "recommended": None}
    matrix  = json.loads(COMPAT_MATRIX_PATH.read_text())
    entries = matrix.get("dcgm_exporter", [])
    recommended = next(
        (e["version"] for e in entries if e.get("recommended")),
        entries[0]["version"] if entries else None
    )
    return {"versions": entries, "recommended": recommended}


# ── DCGM install stream ────────────────────────────────────────────────────────

@router.get("/api/monitoring/dcgm/install/stream")
async def dcgm_install_stream(version: str = ""):
    """
    SSE stream: deploy DCGM Exporter DaemonSet via ansible-monitoring.
    Runs the full site.yml with deploy_dcgm=true and the chosen dcgm_version.
    chart_version is required alongside dcgm_version because site.yml always
    runs the prometheus_stack play first — Helm is idempotent so this is safe.
    """
    if not version:
        return JSONResponse(
            status_code=400,
            content={"error": "dcgm version required"}
        )
    if not DCGM_VERSION_RE.match(version):
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid DCGM version '{version}' — expected format: 3.3.5-3.4.0-ubuntu22.04"}
        )
    if not INVENTORY_PATH.exists():
        return JSONResponse(
            status_code=400,
            content={"error": "No inventory — run Configure first"}
        )
    if not COMPAT_MATRIX_PATH.exists():
        return JSONResponse(
            status_code=400,
            content={"error": "compat_matrix.json not found"}
        )
    # Resolve the current recommended chart_version to pass alongside dcgm_version
    matrix       = json.loads(COMPAT_MATRIX_PATH.read_text())
    stack_entries = matrix.get("kube_prometheus_stack", [])
    chart_version = _match_version(stack_entries, _read_k8s_version())
    if not chart_version and stack_entries:
        chart_version = stack_entries[0]["version"]
    if not chart_version:
        return JSONResponse(
            status_code=400,
            content={"error": "No kube-prometheus-stack version found in compat_matrix.json"}
        )
    return StreamingResponse(
        ansible_stream(
            ANSIBLE_MONITORING_DIR,
            extra_vars={
                "chart_version": chart_version,
                "dcgm_version":  version,
                "deploy_dcgm":   "true",
            }
        ),
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
