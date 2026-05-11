"""
routes/monitoring.py

Handles all monitoring-related API routes for k8s-launcher.

Architecture overview
─────────────────────
The monitoring stack is split into two layers:

Layer 1 — kube-prometheus-stack (Helm chart, always installed)
  The Helm chart installs and manages:
    - Prometheus        : metrics time-series database and query engine
    - Alertmanager      : alert routing, deduplication, and silencing
    - Grafana           : dashboard and visualisation UI
    - Prometheus Operator: manages Prometheus/Alertmanager via CRDs
    - kube-state-metrics : Kubernetes object state as Prometheus metrics
    - node-exporter     : per-node OS/hardware metrics (CPU, RAM, disk, net)
  Ansible does NOT build these from scratch — it calls `helm upgrade --install`
  and passes a values file to customise NodePorts and optional GPU config.

Layer 2 — DCGM Exporter (optional, GPU nodes only)
  The base chart gives us cluster and node metrics, but not GPU telemetry.
  DCGM Exporter is a separate NVIDIA DaemonSet we deploy ourselves.
  It runs only on nodes labelled nvidia.com/gpu.present=true (set by GFD)
  and exposes GPU utilisation, framebuffer memory, temperature, power, and
  clock speed on port 9400. Prometheus scrapes those metrics; Grafana shows
  them in the NVIDIA dashboard (gnetId 12239).

Install flows
─────────────
Base install:   GET /api/monitoring/install/stream
  → ansible-monitoring site.yml with deploy_dcgm=false
  → Helm installs/upgrades kube-prometheus-stack
  → DCGM blocks are NOT rendered in values.yaml.j2

DCGM install:   GET /api/monitoring/dcgm/install/stream
  → ansible-monitoring site.yml with deploy_dcgm=true
  → Helm upgrades kube-prometheus-stack (idempotent, adds DCGM config)
  → dcgm_exporter role deploys the DaemonSet
  → Grafana NVIDIA dashboard is provisioned automatically

Concurrency protection
──────────────────────
Both install streams run ansible-playbook as a subprocess. Running two
installs concurrently causes Helm's "another operation in progress" error.

Protection is two-layered:
  - Frontend: setMonitoringActionsDisabled(true/false) disables both buttons
    for the duration of any active stream.
  - Backend:  _monitoring_lock (threading.Lock) is the authoritative guard.
    If the lock is already held when a stream is requested, the generator
    immediately emits the stable SSE token __ERROR__:LOCKED and exits.
    There is no 409 response on these routes — lock contention is always
    surfaced through the SSE stream so the frontend onmessage handler
    (not onerror) receives it. This keeps the error-handling contract clean.

SSE error token contract
────────────────────────
  __DONE__           — operation completed successfully
  __ERROR__:LOCKED   — another monitoring operation is already in progress
  __ERROR__:<other>  — operation failed (exit code or runtime error)
"""

import json
import re
import threading
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

# ── Regex validators ───────────────────────────────────────────────────────────
VERSION_RE      = re.compile(r"^\d+\.\d+\.\d+$")
DCGM_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+-\d+\.\d+\.\d+-ubuntu\d+\.\d+$")

# ── Concurrency lock ───────────────────────────────────────────────────────────
# Shared across BOTH install routes. Only one monitoring ansible-playbook
# may run at a time. Lock contention is communicated via __ERROR__:LOCKED
# in the SSE stream — never via an HTTP 409 — so the frontend onmessage
# handler is the single place that processes all terminal stream states.
_monitoring_lock = threading.Lock()


def _locked_ansible_stream(ansible_dir, extra_vars: dict):
    """
    Wrap ansible_stream with the monitoring lock.

    If the lock is already held, immediately emits __ERROR__:LOCKED and
    returns — the stream opens normally (HTTP 200) and the frontend
    onmessage handler catches the stable token.

    The lock is released in the finally block regardless of whether the
    stream completed normally, the client disconnected, or an exception
    occurred inside ansible_stream.
    """
    if not _monitoring_lock.acquire(blocking=False):
        yield "data: __ERROR__:LOCKED\n\n"
        return
    try:
        yield from ansible_stream(ansible_dir, extra_vars=extra_vars)
    finally:
        _monitoring_lock.release()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _strip_header(out: str) -> str:
    """Remove the Ansible ad-hoc header line from run_on_cp output."""
    lines = out.strip().splitlines()
    for i, line in enumerate(lines):
        if " | " in line and " | rc=" in line:
            return "\n".join(lines[i + 1:]).strip()
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
    SSE stream: install or upgrade kube-prometheus-stack.

    Lock contention is communicated via __ERROR__:LOCKED in the stream,
    not via HTTP 409, so the frontend onmessage handler is the single
    place that processes all terminal states (done, locked, failed).
    """
    if not version:
        return JSONResponse(status_code=400, content={"error": "version required"})
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
        _locked_ansible_stream(
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


# ── DCGM versions ──────────────────────────────────────────────────────────────

@router.get("/api/monitoring/dcgm/versions")
async def dcgm_versions():
    """
    Return DCGM Exporter image versions from compat_matrix.json.
    DCGM compatibility is driver-based, not k8s-version-based.
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
    SSE stream: deploy DCGM Exporter DaemonSet.

    Runs site.yml with deploy_dcgm=true. The prometheus_stack role runs
    first (Helm is idempotent — if nothing changed, it exits in seconds),
    then dcgm_exporter role deploys the DaemonSet and Grafana receives
    the NVIDIA dashboard automatically via dashboard provisioning.

    Lock contention is communicated via __ERROR__:LOCKED in the stream.
    """
    if not version:
        return JSONResponse(status_code=400, content={"error": "dcgm version required"})
    if not DCGM_VERSION_RE.match(version):
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid DCGM version '{version}' — expected format: 3.3.5-3.4.0-ubuntu22.04"}
        )
    if not INVENTORY_PATH.exists():
        return JSONResponse(status_code=400, content={"error": "No inventory — run Configure first"})
    if not COMPAT_MATRIX_PATH.exists():
        return JSONResponse(status_code=400, content={"error": "compat_matrix.json not found"})

    matrix        = json.loads(COMPAT_MATRIX_PATH.read_text())
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
        _locked_ansible_stream(
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