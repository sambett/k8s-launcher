"""
routes/monitoring.py

Handles all monitoring-related API routes for k8s-launcher.

Architecture overview
---------------------
The monitoring stack has two layers:

Layer 1 - kube-prometheus-stack
  Helm installs and manages Prometheus, Alertmanager, Grafana, the
  Prometheus Operator, kube-state-metrics, and node-exporter.

Layer 2 - DCGM Exporter (optional)
  DCGM Exporter is a separate NVIDIA DaemonSet for GPU telemetry. When
  enabled, Prometheus scrapes its metrics and Grafana provisions the GPU
  dashboard automatically.

Install flow
------------
GET /api/monitoring/install/stream
  - always installs/upgrades kube-prometheus-stack using the requested chart
  - when deploy_dcgm=false, GPU-specific config is omitted
  - when deploy_dcgm=true, dcgm_version is required and the same playbook
    also deploys the DCGM Exporter DaemonSet

Concurrency protection
----------------------
The install stream runs ansible-playbook as a subprocess. Running two installs
concurrently causes Helm's "another operation in progress" error.

Protection is two-layered:
  - Frontend: disables the install action while a stream is active
  - Backend: _monitoring_lock is the authoritative guard

SSE error token contract
------------------------
  __DONE__           - operation completed successfully
  __ERROR__:LOCKED   - another monitoring operation is already in progress
  __ERROR__:<other>  - operation failed
"""

import json
import re
import threading

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from core.ansible import ansible_stream, read_cp_ip, run_on_cp
from core.paths import (
    ANSIBLE_MONITORING_DIR,
    COMPAT_MATRIX_PATH,
    INVENTORY_PATH,
    VARS_PATH,
)

router = APIRouter()

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
DCGM_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+-\d+\.\d+\.\d+-ubuntu\d+\.\d+$")

# Shared across monitoring installs. Only one monitoring ansible-playbook
# may run at a time.
_monitoring_lock = threading.Lock()


def _locked_ansible_stream(ansible_dir, extra_vars: dict):
    """Wrap ansible_stream with the monitoring lock."""
    if not _monitoring_lock.acquire(blocking=False):
        yield "data: __ERROR__:LOCKED\n\n"
        return
    try:
        yield from ansible_stream(ansible_dir, extra_vars=extra_vars)
    finally:
        _monitoring_lock.release()


def _strip_header(out: str) -> str:
    """Remove the Ansible ad-hoc header line from run_on_cp output."""
    lines = out.strip().splitlines()
    for i, line in enumerate(lines):
        if " | " in line and " | rc=" in line:
            return "\n".join(lines[i + 1 :]).strip()
    return out.strip()


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
    parts = clean.split()
    chart = parts[5] if len(parts) > 5 else "unknown"
    return {"status": "installed", "chart": chart}


@router.get("/api/monitoring/versions")
async def monitoring_versions():
    """
    Return kube-prometheus-stack versions from compat_matrix.json.
    Marks the recommended version based on the current cluster k8s minor.
    """
    if not COMPAT_MATRIX_PATH.exists():
        return {"versions": [], "recommended": None, "k8s_version": ""}
    matrix = json.loads(COMPAT_MATRIX_PATH.read_text())
    entries = matrix.get("kube_prometheus_stack", [])
    k8s_ver = _read_k8s_version()
    return {
        "versions": entries,
        "recommended": _match_version(entries, k8s_ver),
        "k8s_version": k8s_ver,
    }


@router.get("/api/monitoring/access")
async def monitoring_access():
    """
    Return live Grafana and Prometheus NodePort URLs.
    Reads CP IP from generated vars file via core.ansible.read_cp_ip().
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


@router.get("/api/monitoring/install/stream")
async def monitoring_install_stream(
    version: str = "",
    deploy_dcgm: bool = False,
    dcgm_version: str = "",
):
    """
    SSE stream: install or upgrade kube-prometheus-stack, optionally with DCGM.

    Lock contention is communicated via __ERROR__:LOCKED in the stream,
    not via HTTP 409, so the frontend onmessage handler is the single
    place that processes all terminal states.
    """
    if not version:
        return JSONResponse(status_code=400, content={"error": "version required"})
    if not VERSION_RE.match(version):
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid version '{version}' - expected X.Y.Z"},
        )
    if not INVENTORY_PATH.exists():
        return JSONResponse(
            status_code=400,
            content={"error": "No inventory - run Configure first"},
        )
    if deploy_dcgm:
        if not dcgm_version:
            return JSONResponse(
                status_code=400,
                content={"error": "dcgm version required"},
            )
        if not DCGM_VERSION_RE.match(dcgm_version):
            return JSONResponse(
                status_code=400,
                content={
                    "error": (
                        f"Invalid DCGM version '{dcgm_version}' - expected format: "
                        "3.3.5-3.4.0-ubuntu22.04"
                    )
                },
            )

    extra_vars = {
        "chart_version": version,
        "deploy_dcgm": str(deploy_dcgm).lower(),
    }
    if deploy_dcgm:
        extra_vars["dcgm_version"] = dcgm_version

    return StreamingResponse(
        _locked_ansible_stream(ANSIBLE_MONITORING_DIR, extra_vars=extra_vars),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/monitoring/dcgm/status")
async def dcgm_status():
    """
    Check whether the DCGM Exporter DaemonSet is running.
    Returns running pod count and desired pod count.
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


@router.get("/api/monitoring/dcgm/versions")
async def dcgm_versions():
    """
    Return DCGM Exporter image versions from compat_matrix.json.
    DCGM compatibility is driver-based, not k8s-version-based.
    """
    if not COMPAT_MATRIX_PATH.exists():
        return {"versions": [], "recommended": None}
    matrix = json.loads(COMPAT_MATRIX_PATH.read_text())
    entries = matrix.get("dcgm_exporter", [])
    recommended = next(
        (e["version"] for e in entries if e.get("recommended")),
        entries[0]["version"] if entries else None,
    )
    return {"versions": entries, "recommended": recommended}
