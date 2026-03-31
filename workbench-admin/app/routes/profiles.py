"""
routes/profiles.py — All /api/profiles/* endpoints.
Handles profile CRUD against the Kubernetes ConfigMap.
Changes only when profile business rules change.
"""

import json
import re
import subprocess
from datetime import datetime

from flask import Blueprint, jsonify, request
from app.auth import require_auth
import app.config as config

bp = Blueprint("profiles", __name__)


# ── ConfigMap helpers ──────────────────────────────────────────────────────────

def read_profiles():
    result = subprocess.run(
        ["kubectl", "get", "configmap", config.CONFIGMAP_NAME,
         "-n", config.CONFIGMAP_NS,
         "-o", "jsonpath={.data.profiles\\.json}"],
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
        ["kubectl", "patch", "configmap", config.CONFIGMAP_NAME,
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

    profiles = read_profiles()
    if any(x["slug"] == p["slug"] for x in profiles):
        return jsonify({"error": f"Slug '{p['slug']}' already exists"}), 409

    profiles.append({
        "slug":          p["slug"],
        "display_name":  p["display_name"],
        "description":   p.get("description", ""),
        "gitlab_group":  p["gitlab_group"],
        "image_name":    p["image_name"],
        "image_options": p["image_options"],
        "cpu_limit":     p["cpu_limit"],
        "cpu_guarantee": p["cpu_guarantee"],
        "mem_limit":     p["mem_limit"],
        "mem_guarantee": p["mem_guarantee"],
        "gpu":           p.get("gpu", 0),
        "created_at":    datetime.utcnow().isoformat(),
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
