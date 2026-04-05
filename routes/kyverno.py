"""
routes/kyverno.py — Kyverno policy engine: version selection, install, status, policies.

  GET /api/kyverno/versions          — compat_matrix filter by live k8s version
  GET /api/kyverno/install/stream    — SSE: install via Ansible
  GET /api/kyverno/status            — pod-level health (core pods only)
  GET /api/kyverno/policies          — active ClusterPolicies with rich detail

Policy CRUD lives in workbench-admin (Day-2). This file is Day-0 only.
"""
import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from core.ansible import ansible_stream, run_on_cp
from core.paths import BASE_DIR, ANSIBLE_KYVERNO_DIR

router = APIRouter()

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")   # Helm chart versions: X.Y.Z


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_k8s_minor() -> str:
    """Return current cluster minor version as '1.30' string."""
    out, _ = run_on_cp(
        "kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.kubeletVersion}'"
    )
    m = re.search(r"v?(\d+\.\d+)", out)
    return m.group(1) if m else ""


def _strip_ansible_header(out: str) -> str:
    """
    Remove the Ansible ad-hoc metadata header line from command output.

    Ansible prints one header per targeted host before the real output:
        ansiblecplane | SUCCESS | rc=0 >>
        <actual output>

    The header always contains ' | rc=' which is unique enough to identify it.
    Everything from the line AFTER that header onwards is the real output.
    """
    lines = out.strip().splitlines()
    for i, line in enumerate(lines):
        if " | " in line and " | rc=" in line:
            return "\n".join(lines[i + 1:])
    return out   # no header found — return as-is


def _compute_age(timestamp_str: str) -> str:
    """Convert ISO-8601 creationTimestamp to a human-readable age string."""
    try:
        created = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        delta   = datetime.now(timezone.utc) - created
        s = int(delta.total_seconds())
        if s < 60:      return f"{s}s"
        if s < 3600:    return f"{s // 60}m"
        if s < 86400:   return f"{s // 3600}h"
        return f"{s // 86400}d"
    except Exception:
        return "unknown"


def _is_pod_ready(pod: dict) -> bool:
    """
    Return True only if a pod is both Running AND all containers are ready.

    We check the 'ready' field (e.g. '1/1', '2/2') rather than just 'status'
    because a pod can be Running but 0/1 ready during startup.
    """
    if pod.get("status") != "Running":
        return False
    try:
        r, t = pod["ready"].split("/")
        return int(r) == int(t)
    except Exception:
        return False


# ── Core Kyverno pod name prefixes ──────────────────────────────────────────────
# These are the 4 permanent Kyverno controller deployments.
# Any other pod in the kyverno namespace is a CronJob-spawned cleanup job,
# which can fail ImagePullBackOff in air-gapped clusters — that is normal.
_CORE_PREFIXES = (
    "kyverno-admission-controller",
    "kyverno-background-controller",
    "kyverno-cleanup-controller",
    "kyverno-reports-controller",
)


# ── Versions ───────────────────────────────────────────────────────────────────

@router.get("/api/kyverno/versions")
async def kyverno_versions():
    """
    Return Kyverno Helm chart versions compatible with the current cluster.
    Reads compat_matrix.json, filters by k8s_min/k8s_max, marks recommended.
    """
    try:
        matrix = json.loads((BASE_DIR / "compat_matrix.json").read_text())
    except Exception as e:
        return {"versions": [], "k8s_version": "", "recommended": None, "error": str(e)}

    k8s_version  = _get_k8s_minor()
    all_versions = matrix.get("kyverno", [])
    compatible   = []
    recommended  = None

    for v in all_versions:
        if k8s_version:
            try:
                kv = tuple(int(x) for x in k8s_version.split("."))
                mn = tuple(int(x) for x in v.get("k8s_min", "0.0").split("."))
                mx = tuple(int(x) for x in v.get("k8s_max", "99.99").split("."))
                if kv < mn or kv > mx:
                    continue
            except Exception:
                pass
        compatible.append(v)
        if recommended is None:
            recommended = v["version"]

    return {
        "versions":    compatible,
        "k8s_version": k8s_version,
        "recommended": recommended,
    }


# ── Install ────────────────────────────────────────────────────────────────────

@router.get("/api/kyverno/install/stream")
async def kyverno_install_stream(version: str):
    """SSE endpoint: install Kyverno via ansible-kyverno playbook."""

    async def _stream():
        if not VERSION_RE.match(version):
            yield f"data: __ERROR__ Invalid version '{version}' — expected X.Y.Z (e.g. 3.2.6)\n\n"
            return
        yield f"data: Installing Kyverno {version}...\n\n"
        for chunk in ansible_stream(
            ANSIBLE_KYVERNO_DIR,
            extra_vars={"kyverno_version": version}
        ):
            yield chunk

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Status ─────────────────────────────────────────────────────────────────────

@router.get("/api/kyverno/status")
async def kyverno_status():
    """
    Return the health of all Kyverno pods with a clear categorisation:

      core_pods  — the 4 permanent controller deployments that must all be
                   Running AND ready for Kyverno to enforce policy.
      job_pods   — cleanup CronJob pods.  These can fail (ImagePullBackOff)
                   in air-gapped / offline clusters — that is expected and
                   does NOT affect policy enforcement.

    Fixes vs previous version:
    - Removed -l '!batch.kubernetes.io/job-name' label selector (the '!'
      triggers bash history expansion when routed through Ansible shell,
      producing unreliable results).
    - Filtering is now done in Python by matching pod name prefixes.
    - 'running' is True only when all core pods are Running AND N/N ready.
    - 'message' explains the reason when status is not fully healthy.
    """
    out, rc = run_on_cp(
        "kubectl get pods -n kyverno --no-headers 2>&1"
    )

    if rc != 0 or not out.strip() or "No resources found" in out:
        return {
            "installed":  False,
            "running":    False,
            "core_pods":  [],
            "job_pods":   [],
            "message":    "No Kyverno pods found — is Kyverno installed?",
        }

    # ── Parse raw kubectl output ──────────────────────────────────────────────
    # Strip the Ansible header line before processing
    clean = _strip_ansible_header(out)

    core_pods = []
    job_pods  = []

    for line in clean.strip().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        pod = {"name": parts[0], "ready": parts[1], "status": parts[2]}

        if any(pod["name"].startswith(prefix) for prefix in _CORE_PREFIXES):
            core_pods.append(pod)
        else:
            job_pods.append(pod)

    # ── Determine overall health ──────────────────────────────────────────────
    all_core_ready = (
        bool(core_pods) and all(_is_pod_ready(p) for p in core_pods)
    )

    # ── Build explanation message when not fully ready ────────────────────────
    message = ""
    if core_pods and not all_core_ready:
        not_ready = [p for p in core_pods if not _is_pod_ready(p)]
        # Shorten pod names for readability:
        # "kyverno-admission-controller-6d7b9f-xxxx" → "admission-controller"
        short_names = []
        for p in not_ready:
            name = p["name"]
            for prefix in _CORE_PREFIXES:
                if name.startswith(prefix):
                    # keep just the controller type, drop the hash suffix
                    short_names.append(prefix.replace("kyverno-", ""))
                    break
            else:
                short_names.append(name.split("-")[0])

        if all(p["status"] == "Running" for p in not_ready):
            message = (
                "{} of {} core pod(s) Running but not yet ready ({}). "
                "This is normal right after installation — wait 30–60 seconds and refresh."
            ).format(len(not_ready), len(core_pods), ", ".join(short_names))
        else:
            statuses = [p["status"] for p in not_ready]
            message = (
                "{} of {} core pod(s) not healthy: {} — status: {}. "
                "Check cluster resources or re-run the install."
            ).format(
                len(not_ready), len(core_pods),
                ", ".join(short_names),
                ", ".join(statuses)
            )

    return {
        "installed":  len(core_pods) > 0,
        "running":    all_core_ready,
        "core_pods":  core_pods,
        "job_pods":   job_pods,
        "message":    message,
    }


# ── Policies ───────────────────────────────────────────────────────────────────

@router.get("/api/kyverno/policies")
async def list_policies():
    """
    Return active ClusterPolicies with rich detail extracted from the Kyverno API.

    Each policy object contains:
      name        — internal Kubernetes name (e.g. jupyterhub-gpu-group-policy)
      title       — human-readable title from annotation (e.g. JupyterHub GPU Group Quota)
      description — description from annotation (empty string if absent)
      ready       — lowercase 'true' / 'false' / 'unknown'
      action      — 'Enforce' (hard block) or 'Audit' (log only)
      rule_count  — number of rules inside this policy
      groups      — list of GitLab groups covered by at least one rule
      age         — human-readable age (e.g. 5m, 2h, 3d)

    Fixes vs previous version:
    - Ansible header line is stripped before parsing (was producing a fake
      policy entry named 'ansiblecplane' from the ad-hoc metadata line).
    - Uses -o json to extract title, description, action, rules, and groups
      rather than just the --no-headers table with 3 columns.
    - 'ready' is always lowercase so JS === 'true' works correctly.
    """
    # ── Fetch JSON representation of all ClusterPolicies ─────────────────────
    out, rc = run_on_cp("kubectl get clusterpolicies -o json 2>&1")

    if rc != 0 or not out.strip():
        return {"policies": [], "error": "kubectl get clusterpolicies failed"}

    clean = _strip_ansible_header(out)
    if not clean.strip():
        return {"policies": []}

    # Guard: "No resources found" appears in the clean output (not the header)
    if "No resources found" in clean or "not found" in clean.lower():
        return {"policies": []}

    try:
        data = json.loads(clean)
    except json.JSONDecodeError as e:
        return {
            "policies": [],
            "error":    f"Could not parse ClusterPolicy JSON: {e}"
        }

    items = []
    # kubectl get X -o json returns {"kind":"List","items":[...]}
    # or just the single object if there's one; handle both
    if isinstance(data, dict):
        items = data.get("items", [data] if data.get("kind") == "ClusterPolicy" else [])
    elif isinstance(data, list):
        items = data

    policies = []
    for item in items:
        if not isinstance(item, dict):
            continue

        meta        = item.get("metadata", {})
        spec        = item.get("spec", {})
        status      = item.get("status", {})
        annotations = meta.get("annotations", {})

        name        = meta.get("name", "unknown")
        ready_raw   = status.get("ready", False)
        ready       = str(ready_raw).lower()           # always lowercase
        action      = spec.get("validationFailureAction", "unknown")
        rules       = spec.get("rules", [])
        rule_count  = len(rules)
        title       = annotations.get(
            "policies.kyverno.io/title",
            # Fallback: convert slug to readable words
            name.replace("-", " ").title()
        )
        description = annotations.get("policies.kyverno.io/description", "")
        created     = meta.get("creationTimestamp", "")
        age         = _compute_age(created)

        # ── Extract which groups this policy covers ───────────────────────────
        groups = []
        for rule in rules:
            for any_block in (rule.get("match", {}).get("any", []) or []):
                labels = (
                    any_block
                    .get("resources", {})
                    .get("selector", {})
                    .get("matchLabels", {})
                )
                group = labels.get("workbench/gitlab-group", "")
                if group and group not in groups:
                    groups.append(group)

        policies.append({
            "name":        name,
            "title":       title,
            "description": description,
            "ready":       ready,
            "action":      action,
            "rule_count":  rule_count,
            "groups":      groups,
            "age":         age,
        })

    return {"policies": policies}
