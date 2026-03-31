"""
gitlab_client.py — The only place that talks to GitLab.
All GitLab API calls go through here.
Token is re-read from disk on every call — enables hot-swap with no restart.
Changes only when GitLab API structure changes or token storage backend changes.
"""

import json
import os
import urllib.request
import urllib.error
import app.config as config


def get_token():
    """
    Read the GitLab token fresh on every call.
    Checks secrets.env first, falls back to environment variable.
    """
    if os.path.exists(config.SECRETS_ENV_PATH):
        with open(config.SECRETS_ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GITLAB_TOKEN="):
                    return line.split("=", 1)[1]
    return os.environ.get("GITLAB_TOKEN", "")


def gitlab_get(path):
    """Authenticated GET request to GitLab API. Returns parsed JSON or error dict."""
    url = f"{config.gitlab_url()}/api/v4/{path}"
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": get_token()})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def gitlab_delete(path):
    """
    Authenticated DELETE request to GitLab API.
    Returns {"success": True} on 200/202/204, or {"error": str} on failure.
    """
    url = f"{config.gitlab_url()}/api/v4/{path}"
    req = urllib.request.Request(
        url,
        method="DELETE",
        headers={"PRIVATE-TOKEN": get_token()}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"success": True, "status": resp.status}
    except urllib.error.HTTPError as e:
        return {"error": f"GitLab returned HTTP {e.code}", "code": e.code}
    except Exception as e:
        return {"error": str(e)}


def test_token(token):
    """
    Validate a token against GitLab by calling /api/v4/user.
    On success: {"valid": True, "username": "...", "name": "..."}
    On failure: {"valid": False, "reason": "..."}
    """
    url = f"{config.gitlab_url()}/api/v4/user"
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": token})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
            return {
                "valid":    True,
                "username": data.get("username", "unknown"),
                "name":     data.get("name", "unknown"),
            }
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"valid": False, "reason": "Token invalid or expired (401)"}
        return {"valid": False, "reason": f"GitLab returned HTTP {e.code}"}
    except Exception as e:
        return {"valid": False, "reason": str(e)}


def save_token(token):
    """
    Write a validated token to secrets.env.
    Only call this after test_token() returns valid=True.
    """
    with open(config.SECRETS_ENV_PATH, "w") as f:
        f.write(f"GITLAB_TOKEN={token}\n")


def gitlab_post(path, body):
    """
    Authenticated POST request to GitLab API.
    Returns parsed JSON on success, or {"error": str} on failure.
    """
    import json as _json
    url  = f"{config.gitlab_url()}/api/v4/{path}"
    data = _json.dumps(body).encode()
    req  = urllib.request.Request(
        url,
        data=data,
        headers={
            "PRIVATE-TOKEN": get_token(),
            "Content-Type":  "application/json",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            detail = _json.loads(body)
        except Exception:
            detail = body
        return {"error": f"GitLab returned HTTP {e.code}", "detail": detail}
    except Exception as e:
        return {"error": str(e)}
