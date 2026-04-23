"""
routes/profiles.py — All /api/profiles/* endpoints.
Handles profile CRUD against the Kubernetes ConfigMap.

Profile types:
  cpu — no GPU, no nodeSelector injected into KubeSpawner
  gpu — always requests 1 nvidia.com/gpu, nodeSelector set to
        nvidia.com/gpu.product=<gpu_model> for exact hardware placement

The 'gpu' field in the stored schema is always derived:
  profile_type == 'gpu' → gpu = 1
  profile_type == 'cpu' → gpu = 0
It is never accepted as a raw user input.
"""

import json
import re
import shutil
import subprocess
from datetime import datetime

from flask import Blueprint, jsonify, request
from app.auth import require_auth
import app.config as config

bp = Blueprint("profiles", __name__)

_KUBECTL = shutil.which("kubectl") or "/usr/local/bin/kubectl"

# Minimum RAM in Gi below which a GPU profile is rejected server-side.
# The UI enforces this too, but server-side validation is the authoritative gate.
_RAM_SUFFIXES = {"gi": 1, "g": 1, "mi": 1/1024, "m": 1/1024}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_ram_gi(val):
    """Parse a RAM string like '8Gi', '8G', '8192Mi' → float in Gi."""
    s = str(val).strip()
    m = re.match(r'^([0-9.]+)\s*(Gi|G|Mi|M)?$', s, re.IGNORECASE)
    if not m:
        return 0.0
    n   = float(m.group(1))
    suf = (m.group(2) or "gi").lower()
    return n * _RAM_SUFFIXES.get(suf, 1)


def _validate_gpu_floors(profile, matrix):
    """
    Validate that CPU guarantee and RAM guarantee meet the matrix floor
    for the selected GPU model. Returns (ok: bool, error: str).
    matrix is a list of dicts from the gpu-compat-matrix ConfigMap.
    """
    gpu_model = profile.get("gpu_model", "")
    if not gpu_model:
        return False, "gpu_model is required for GPU profiles"

    entry = next((m for m in matrix if m.get("gpu_model") == gpu_model), None)
    if not entry:
        # Model not in matrix — allow but don't enforce floors
        return True, ""

    min_cpu = float(entry.get("min_cpu", 0) or 0)
    min_ram = _parse_ram_gi(entry.get("min_ram", "0") or "0")

    cpu_req = float(profile.get("cpu_guarantee", 0) or 0)
    ram_req = _parse_ram_gi(profile.get("mem_guarantee", "0") or "0")

    if min_cpu > 0 and cpu_req < min_cpu:
        return False, (
            f"CPU guarantee {cpu_req} is below the floor of {min_cpu} cores "
            f"for GPU model {gpu_model}"
        )
    if min_ram > 0 and ram_req < min_ram:
        return False, (
            f"RAM guarantee {profile.get('mem_guarantee')} is below the floor of "
            f"{entry.get('min_ram')} for GPU model {gpu_model}"
        )
    return True, ""


def _read_gpu_matrix():
    """Read the gpu-compat-matrix ConfigMap. Returns list or empty list."""
    r = subprocess.run(
        [_KUBECTL, "get", "configmap", "gpu-compat-matrix",
         "-n", config.CONFIGMAP_NS,
         "-o", "jsonpath={.data.matrix}"],
        capture_output=True, text=True
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        return json.loads(r.stdout.strip())
    except Exception:
        return []


# ── ConfigMap helpers ──────────────────────────────────────────────────────────

def read_profiles():
    result = subprocess.run(
        [_KUBECTL, "get", "configmap", config.CONFIGMAP_NAME,
         "-n", config.CONFIGMAP_NS,
         "-o", r"jsonpath={.data.profiles\.json}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout.strip())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def write_profiles(profiles):
    json_str = json.dumps(profiles, indent=2)
    patch    = {"data": {"profiles.json": json_str}}
    result   = subprocess.run(
        [_KUBECTL, "patch", "configmap", config.CONFIGMAP_NAME,
         "-n", config.CONFIGMAP_NS,
         "--type=merge",
         "--patch", json.dumps(patch)],
        capture_output=True, text=True
    )
    return result.returncode == 0, result.stdout + result.stderr


# ── Routes ─────────────────────────────────────────────────────────────────────

@bp.route("/api/profiles")
@require_auth
def api_list_profiles():
    return jsonify(read_profiles())


@bp.route("/api/profiles", methods=["POST"])
@require_auth
def api_create_profile():
    p = request.json

    required = ["slug", "display_name", "gitlab_group", "image_name", "image_options"]
    for field in required:
        if not p.get(field) and p.get(field) != 0:
            return jsonify({"error": f"Missing field: {field}"}), 400

    if not re.match(r'^[a-z0-9-]+$', p["slug"]):
        return jsonify({"error": "Slug must be lowercase letters, numbers, hyphens only"}), 400

    if not isinstance(p["image_options"], list) or not p["image_options"]:
        return jsonify({"error": "image_options must be a non-empty list"}), 400

    # ── Derive profile_type and gpu ───────────────────────────────────────────
    # Accept profile_type from the request; fall back to legacy gpu field for
    # backward compat with any existing tooling that still sends raw gpu counts.
    profile_type = str(p.get("profile_type", "")).strip().lower()
    if profile_type not in ("cpu", "gpu"):
        # Legacy path: if gpu > 0 treat as gpu profile
        legacy_gpu = int(p.get("gpu", 0) or 0)
        profile_type = "gpu" if legacy_gpu > 0 else "cpu"

    is_gpu = (profile_type == "gpu")

    # GPU field is always derived — never stored as a user-supplied raw value.
    # KubeSpawner reads this as extra_resource_limits.nvidia.com/gpu.
    gpu_count = 1 if is_gpu else 0

    # ── GPU-specific validation ───────────────────────────────────────────────
    gpu_model           = None
    node_selector_key   = None
    node_selector_value = None

    if is_gpu:
        gpu_model = str(p.get("gpu_model", "")).strip()
        if not gpu_model:
            return jsonify({"error": "gpu_model is required for GPU profiles"}), 400

        # node_selector always uses the GFD product label — never accept from client
        node_selector_key   = "nvidia.com/gpu.product"
        node_selector_value = gpu_model

        # Floor validation against compat matrix
        matrix = _read_gpu_matrix()
        ok, err = _validate_gpu_floors(
            {
                "gpu_model":      gpu_model,
                "cpu_guarantee":  p.get("cpu_guarantee", 0),
                "mem_guarantee":  p.get("mem_guarantee", "0"),
            },
            matrix
        )
        if not ok:
            return jsonify({"error": err}), 400

    profiles = read_profiles()
    if any(x["slug"] == p["slug"] for x in profiles):
        return jsonify({"error": f"Slug '{p['slug']}' already exists"}), 409

    profiles.append({
        "slug":                p["slug"],
        "display_name":        p["display_name"],
        "description":         p.get("description", ""),
        "gitlab_group":        p["gitlab_group"],
        "image_name":          p["image_name"],
        "image_options":       p["image_options"],
        "cpu_limit":           p.get("cpu_limit", 2),
        "cpu_guarantee":       p.get("cpu_guarantee", 0.5),
        "mem_limit":           p.get("mem_limit", "4Gi"),
        "mem_guarantee":       p.get("mem_guarantee", "1Gi"),
        # GPU fields — always derived, never raw user input
        "profile_type":        profile_type,
        "gpu":                 gpu_count,
        "gpu_model":           gpu_model,
        "node_selector_key":   node_selector_key,
        "node_selector_value": node_selector_value,
        "created_at":          datetime.utcnow().isoformat(),
    })

    ok, out = write_profiles(profiles)
    if not ok:
        return jsonify({"error": "Failed to write ConfigMap", "detail": out}), 500
    return jsonify({"status": "created", "slug": p["slug"]}), 201


@bp.route("/api/profiles/<slug>", methods=["DELETE"])
@require_auth
def api_delete_profile(slug):
    profiles = read_profiles()
    new_profiles = [p for p in profiles if p["slug"] != slug]
    if len(new_profiles) == len(profiles):
        return jsonify({"error": f"Profile '{slug}' not found"}), 404
    ok, out = write_profiles(new_profiles)
    if not ok:
        return jsonify({"error": "Failed to write ConfigMap", "detail": out}), 500
    return jsonify({"status": "deleted", "slug": slug})


@bp.route("/api/apply", methods=["POST"])
@require_auth
def api_apply():
    profiles = read_profiles()
    ok, out  = write_profiles(profiles)
    if ok:
        return jsonify({
            "success": True,
            "message": (
                f"ConfigMap updated — {len(profiles)} profile(s) active.\n"
                "JupyterHub reflects changes within ~60 seconds.\n"
                "No helm upgrade needed."
            )
        })
    return jsonify({"success": False, "message": out}), 500
