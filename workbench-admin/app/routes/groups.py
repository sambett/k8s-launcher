"""
routes/groups.py — Group management + user creation.
"""

from flask import Blueprint, jsonify, request
from app.auth import require_auth
from app.gitlab_client import gitlab_get, gitlab_post, gitlab_delete

bp = Blueprint("groups", __name__)


# ── User creation ──────────────────────────────────────────────────────────────

@bp.route("/api/users", methods=["POST"])
@require_auth
def api_create_user():
    body     = request.json
    name     = (body.get("name")     or "").strip()
    username = (body.get("username") or "").strip()
    email    = (body.get("email")    or "").strip()
    password = (body.get("password") or "").strip()

    if not name:
        return jsonify({"error": "Missing field: name"}), 400
    if not username:
        return jsonify({"error": "Missing field: username"}), 400
    if not email:
        return jsonify({"error": "Missing field: email"}), 400
    if not password or len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    data = gitlab_post("users", {
        "name":              name,
        "username":          username,
        "email":             email,
        "password":          password,
        "skip_confirmation": True,
        "can_create_group":  False,
    })

    if "error" in data:
        return jsonify(data), 502
    if "id" not in data:
        return jsonify({"error": "Unexpected response", "detail": str(data)}), 500

    return jsonify({
        "id":       data["id"],
        "name":     data["name"],
        "username": data["username"],
        "email":    data["email"],
    }), 201


# ── User search — empty query returns all users ────────────────────────────────

@bp.route("/api/users/search")
@require_auth
def api_search_users():
    q = request.args.get("q", "").strip()
    if q:
        data = gitlab_get(f"users?search={q}&per_page=50")
    else:
        data = gitlab_get("users?per_page=100&active=true")

    if isinstance(data, dict) and "error" in data:
        return jsonify(data), 502

    return jsonify([
        {"id": u["id"], "username": u["username"], "name": u["name"]}
        for u in data
        if u.get("username") != "root"
    ])


# ── Group CRUD ─────────────────────────────────────────────────────────────────

@bp.route("/api/groups")
@require_auth
def api_list_groups():
    data = gitlab_get("groups?all_available=true&per_page=100")
    if isinstance(data, dict) and "error" in data:
        return jsonify(data), 502
    return jsonify([
        {"id": g["id"], "name": g["name"], "path": g["path"], "full_path": g["full_path"]}
        for g in data
        if not g.get("marked_for_deletion_on")
    ])


@bp.route("/api/groups", methods=["POST"])
@require_auth
def api_create_group():
    body = request.json
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Missing field: name"}), 400
    path = name.lower().replace(" ", "-")
    data = gitlab_post("groups", {
        "name":       name,
        "path":       path,
        "visibility": body.get("visibility", "private"),
    })
    if "error" in data:
        return jsonify(data), 502
    if "id" not in data:
        return jsonify({"error": "Unexpected response", "detail": str(data)}), 500
    return jsonify({"id": data["id"], "name": data["name"], "path": data["path"]}), 201


@bp.route("/api/groups/<int:group_id>", methods=["DELETE"])
@require_auth
def api_delete_group(group_id):
    result = gitlab_delete(f"groups/{group_id}")
    if "error" in result:
        return jsonify(result), 500
    return jsonify({"status": "deleted", "id": group_id})


# ── Member management ──────────────────────────────────────────────────────────

@bp.route("/api/groups/<int:group_id>/members")
@require_auth
def api_list_members(group_id):
    data = gitlab_get(f"groups/{group_id}/members?per_page=100")
    if isinstance(data, dict) and "error" in data:
        return jsonify(data), 502
    return jsonify([
        {
            "id":           m["id"],
            "username":     m["username"],
            "name":         m["name"],
            "access_level": m["access_level"],
        }
        for m in data
    ])


@bp.route("/api/groups/<int:group_id>/members", methods=["POST"])
@require_auth
def api_add_member(group_id):
    body     = request.json
    username = (body.get("username") or "").strip()
    role     = body.get("access_level", 30)

    if not username:
        return jsonify({"error": "Missing field: username"}), 400

    users = gitlab_get(f"users?username={username}")
    if isinstance(users, dict) and "error" in users:
        return jsonify(users), 502
    if not users:
        return jsonify({"error": f"User '{username}' not found in GitLab"}), 404

    user_id = users[0]["id"]
    data    = gitlab_post(f"groups/{group_id}/members", {
        "user_id":      user_id,
        "access_level": int(role),
    })
    if "error" in data:
        return jsonify(data), 502
    if "id" not in data:
        return jsonify({"error": "Unexpected response", "detail": str(data)}), 500

    return jsonify({
        "status":       "added",
        "id":           data["id"],
        "username":     data["username"],
        "access_level": data["access_level"],
    }), 201


@bp.route("/api/groups/<int:group_id>/members/<int:user_id>", methods=["DELETE"])
@require_auth
def api_remove_member(group_id, user_id):
    result = gitlab_delete(f"groups/{group_id}/members/{user_id}")
    if "error" in result:
        return jsonify(result), 500
    return jsonify({"status": "removed", "user_id": user_id})


# ── User deletion ──────────────────────────────────────────────────────────────

@bp.route("/api/users/<int:user_id>", methods=["DELETE"])
@require_auth
def api_delete_user(user_id):
    result = gitlab_delete(f"users/{user_id}")
    if "error" in result:
        return jsonify({"error": result["error"]}), 500
    return jsonify({"status": "deleted", "user_id": user_id})
