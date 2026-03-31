"""
routes/registry.py — All /api/registry/* and /api/gitlab/groups endpoints.
Bridges the dashboard to GitLab's registry and group APIs.
Changes only when registry browsing behavior changes.
"""
from flask import Blueprint, jsonify
from app.auth import require_auth
from app.gitlab_client import gitlab_get
import app.config as config

bp = Blueprint("registry", __name__)


@bp.route("/api/gitlab/groups")
@require_auth
def api_groups():
    data = gitlab_get("groups?all_available=true&per_page=100")
    if isinstance(data, dict) and "error" in data:
        return jsonify(data), 502
    return jsonify([
        {
            "id":        g["id"],
            "name":      g["name"],
            "path":      g["path"],
            "full_path": g["full_path"]
        }
        for g in data
        if not g.get("marked_for_deletion_on")
    ])


@bp.route("/api/registry/repos")
@require_auth
def api_repos():
    pid  = config.registry_project_id()
    data = gitlab_get(f"projects/{pid}/registry/repositories")
    if isinstance(data, dict) and "error" in data:
        return jsonify(data), 502

    # If no repositories exist yet (no images pushed),
    # fall back to the configured namespace so the dropdown is never empty.
    if not data:
        conf = config.load_registry_conf()
        host = conf.get("host", "")
        ns   = conf.get("namespace", "registry/notebook-images")
        return jsonify([{
            "id":       None,
            "name":     ns.split("/")[-1],
            "location": f"{host}/{ns}",
            "hint":     "No images pushed yet — this is the configured target"
        }])

    return jsonify([
        {"id": r["id"], "name": r["name"], "location": r["location"]}
        for r in data
    ])


@bp.route("/api/registry/repos/<int:repo_id>/tags")
@require_auth
def api_tags(repo_id):
    pid  = config.registry_project_id()
    data = gitlab_get(f"projects/{pid}/registry/repositories/{repo_id}/tags")
    if isinstance(data, dict) and "error" in data:
        return jsonify(data), 502
    return jsonify([
        {"name": t["name"], "location": t["location"]}
        for t in data
    ])
