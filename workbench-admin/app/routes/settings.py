"""
routes/settings.py — All /api/settings/* endpoints.
Token validation, rotation, and platform info.
All environment-specific values come from registry.conf — never hardcoded.
"""

from flask import Blueprint, jsonify, request
from app.auth import require_auth
from app.gitlab_client import get_token, test_token, save_token
import app.config as config

bp = Blueprint("settings", __name__)


@bp.route("/api/settings/info")
@require_auth
def api_info():
    """
    Returns platform config values for display in the UI.
    Single source of truth: registry.conf — no hardcoded values in templates.
    """
    return jsonify({
        "gitlab_url":   config.gitlab_url(),
        "registry_host": config.load_registry_conf().get("host", ""),
        "secrets_path": config.SECRETS_ENV_PATH,
    })


@bp.route("/api/settings/token", methods=["GET"])
@require_auth
def api_token_status():
    current_token = get_token()
    if not current_token:
        return jsonify({"valid": False, "reason": "No token configured"})
    result = test_token(current_token)
    return jsonify(result)


@bp.route("/api/settings/token", methods=["POST"])
@require_auth
def api_token_rotate():
    body = request.json
    if not body or not body.get("token"):
        return jsonify({"error": "Missing field: token"}), 400

    new_token = body["token"].strip()

    if not new_token.startswith("glpat-"):
        return jsonify({
            "error": "Token format invalid — GitLab personal access tokens start with glpat-"
        }), 400

    result = test_token(new_token)
    if not result["valid"]:
        return jsonify({
            "error":  "Token validation failed — current token unchanged",
            "reason": result["reason"]
        }), 400

    try:
        save_token(new_token)
    except Exception as e:
        return jsonify({
            "error":  "Token validated but failed to save",
            "detail": str(e)
        }), 500

    return jsonify({
        "status":   "saved",
        "username": result["username"],
        "name":     result["name"],
        "message":  f"Token saved — authenticated as {result['username']}"
    })
