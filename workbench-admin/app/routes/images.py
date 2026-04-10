"""
routes/images.py — All /api/images/* endpoints.
Handles image import and tag deletion for the GitLab registry.
Delete is blocked if any profile references the tag — prevents orphaned profiles.
Import runs as a background thread — HTTP returns immediately with a job ID.
"""

import shutil
import subprocess
import threading
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request
from app.auth import require_auth
from app.config import load_registry_conf, registry_project_id
from app.gitlab_client import get_token, gitlab_delete
from app.routes.profiles import read_profiles

bp = Blueprint("images", __name__)

# ── In-memory job store ────────────────────────────────────────────────────────
_jobs = {}
_jobs_lock = threading.Lock()

# Maximum number of completed jobs to keep in memory.
# Running jobs are never pruned regardless of this limit.
_JOBS_MAX = 20

# Resolve skopeo once at module load — avoids PATH lookup on every import call.
# Falls back to the conventional install location if not found on PATH.
_SKOPEO = shutil.which("skopeo") or "/usr/bin/skopeo"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _registry_host():
    return load_registry_conf().get("host", "10.110.188.61:5050")


def _registry_namespace():
    return load_registry_conf().get("namespace", "registry/notebook-images")


def _append_log(job_id, line):
    with _jobs_lock:
        _jobs[job_id]["log"].append(line)


def _prune_jobs():
    """
    Remove the oldest finished jobs once the store exceeds _JOBS_MAX entries.
    Called inside _jobs_lock — must not acquire the lock itself.
    Only jobs in a terminal state (done/failed) are eligible for removal.
    Running jobs are always preserved regardless of the cap.
    """
    finished = [
        jid for jid, j in _jobs.items()
        if j["status"] in ("done", "failed")
    ]
    for jid in finished[:max(0, len(_jobs) - _JOBS_MAX)]:
        del _jobs[jid]


def _profiles_using_tag(repo_name, tag_name):
    """
    Return list of profiles that reference this repo+tag combination.
    A profile matches if:
      - its image_name == repo_name  (e.g. "notebook-cpu")
      - AND tag_name is in its image_options list
    """
    profiles = read_profiles()
    blocking = []
    for p in profiles:
        if p.get("image_name") == repo_name:
            options = p.get("image_options", [])
            if tag_name in options:
                blocking.append({
                    "slug":         p["slug"],
                    "display_name": p["display_name"],
                    "gitlab_group": p["gitlab_group"],
                })
    return blocking


def run_import(job_id, source, target):
    """
    Copy image from source registry to GitLab registry using skopeo.
    To swap backend: replace this function only.
    """
    token = get_token()
    cmd = [
        _SKOPEO, "copy",
        f"docker://{source}",
        f"docker://{target}",
        "--dest-creds", f"root:{token}",
        "--dest-tls-verify=false",
        "--src-tls-verify=true",
    ]

    safe_cmd = " ".join(cmd).replace(token, "****")
    _append_log(job_id, f"▶ Running: {safe_cmd}")
    _append_log(job_id, f"  Source: {source}")
    _append_log(job_id, f"  Target: {target}")
    _append_log(job_id, "")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        for line in proc.stdout:
            _append_log(job_id, line.rstrip())
        proc.wait()

        if proc.returncode != 0:
            _append_log(job_id, f"\n✗ skopeo exited with code {proc.returncode}")
            with _jobs_lock:
                _jobs[job_id]["status"] = "failed"
                _prune_jobs()
        else:
            with _jobs_lock:
                _jobs[job_id]["status"] = "done"
                _prune_jobs()
            _append_log(job_id, f"\n✓ Import complete → {target}")

    except FileNotFoundError:
        _append_log(job_id, f"✗ skopeo not found at {_SKOPEO} — run: sudo apt-get install -y skopeo")
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _prune_jobs()
    except Exception as e:
        _append_log(job_id, f"✗ Unexpected error: {str(e)}")
        with _jobs_lock:
            _jobs[job_id]["status"] = "failed"
            _prune_jobs()


# ── Routes ─────────────────────────────────────────────────────────────────────

@bp.route("/api/images/import", methods=["POST"])
@require_auth
def api_import_start():
    body = request.json
    if not body:
        return jsonify({"error": "Request body required"}), 400

    source      = (body.get("source")      or "").strip()
    target_repo = (body.get("target_repo") or "").strip()
    target_tag  = (body.get("target_tag")  or "").strip()

    if not source:
        return jsonify({"error": "Missing field: source"}), 400
    if not target_repo:
        return jsonify({"error": "Missing field: target_repo"}), 400
    if not target_tag:
        return jsonify({"error": "Missing field: target_tag"}), 400

    target = f"{_registry_host()}/{_registry_namespace()}/{target_repo}:{target_tag}"

    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "status":     "running",
            "log":        [f"Job started at {datetime.utcnow().isoformat()}Z"],
            "target":     target,
            "source":     source,
            "started_at": datetime.utcnow().isoformat(),
        }

    t = threading.Thread(target=run_import, args=(job_id, source, target), daemon=True)
    t.start()

    return jsonify({
        "job_id": job_id,
        "target": target,
        "status": "running"
    }), 202


@bp.route("/api/images/import/<job_id>", methods=["GET"])
@require_auth
def api_import_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": f"Job '{job_id}' not found"}), 404
    return jsonify({
        "job_id":     job_id,
        "status":     job["status"],
        "log":        job["log"],
        "target":     job["target"],
        "source":     job["source"],
        "started_at": job["started_at"],
    })


@bp.route("/api/images/tags/<int:repo_id>/<tag_name>", methods=["DELETE"])
@require_auth
def api_delete_tag(repo_id, tag_name):
    """
    Delete a specific tag from the GitLab registry.
    Blocked if any profile references this tag — prevents orphaned profiles.
    The repo_name is passed as a query param so we can check profiles.
    Example: DELETE /api/images/tags/4/scipy-2024?repo_name=notebook-cpu
    """
    repo_name = request.args.get("repo_name", "")

    if repo_name:
        blocking = _profiles_using_tag(repo_name, tag_name)
        if blocking:
            return jsonify({
                "error":    f"Tag '{tag_name}' is used by {len(blocking)} profile(s)",
                "blocking": blocking,
                "hint":     "Delete these profiles first, then retry."
            }), 409

    pid    = registry_project_id()
    path   = f"projects/{pid}/registry/repositories/{repo_id}/tags/{tag_name}"
    result = gitlab_delete(path)

    if "error" in result:
        return jsonify({
            "error":  f"Failed to delete tag '{tag_name}'",
            "detail": result["error"]
        }), 500

    return jsonify({
        "status":  "deleted",
        "repo_id": repo_id,
        "tag":     tag_name,
        "message": f"Tag '{tag_name}' deleted successfully"
    })


@bp.route("/api/images/repos/<int:repo_id>", methods=["DELETE"])
@require_auth
def api_delete_repo(repo_id):
    """
    Delete an entire registry repository and all its tags.
    Blocked if any profile references this repository.
    """
    repo_name = request.args.get("repo_name", "")

    if repo_name:
        profiles = read_profiles()
        blocking = [
            {"slug": p["slug"], "display_name": p["display_name"]}
            for p in profiles
            if p.get("image_name") == repo_name
        ]
        if blocking:
            return jsonify({
                "error":    f"Repository '{repo_name}' is used by {len(blocking)} profile(s)",
                "blocking": blocking,
                "hint":     "Delete these profiles first, then retry."
            }), 409

    pid    = registry_project_id()
    path   = f"projects/{pid}/registry/repositories/{repo_id}"
    result = gitlab_delete(path)

    if "error" in result:
        return jsonify({
            "error":  f"Failed to delete repository '{repo_name}'",
            "detail": result["error"]
        }), 500

    return jsonify({
        "status":  "deleted",
        "repo_id": repo_id,
        "message": f"Repository '{repo_name}' deleted successfully"
    })
