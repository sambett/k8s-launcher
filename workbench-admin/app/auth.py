"""
auth.py — HTTP Basic Auth decorator.
"""
import base64
from functools import wraps
from flask import request, Response
import app.config as config


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            authenticated = False
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                username, password = decoded.split(":", 1)
                if username == config.DASHBOARD_USER and password == config.DASHBOARD_PASS:
                    authenticated = True
            except Exception:
                pass
            if authenticated:
                return f(*args, **kwargs)
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="AI Workbench Admin"'}
        )
    return decorated
