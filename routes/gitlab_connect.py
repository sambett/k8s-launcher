"""
routes/gitlab_connect.py — "Attach existing GitLab" flow.

Completely separate from routes/gitlab.py (fresh GitLab install).
Never touches the GitLab VM — only calls GitLab APIs.
Produces the same generated/gitlab-outputs.json and generated/gitlab-vars.yml
that the fresh-install flow produces, so JupyterHub and Dashboard tabs work
identically regardless of which flow ran.

POST /api/gitlab/connect/configure  — validate PAT + store params
GET  /api/gitlab/connect/stream     — create resources, write outputs,
                                      configure registry on cluster nodes
"""
import json
import re
import subprocess
import urllib.error
import urllib.request

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.paths import (
    ANSIBLE_K8S_DIR,
    GITLAB_OUTPUTS_PATH,
    GITLAB_VARS_PATH,
    INVENTORY_PATH,
    VARS_PATH,
    GENERATED_DIR,
)

router = APIRouter()

# Stores validated connect params between configure POST and stream GET.
# Deleted automatically after a successful stream run.
_CONNECT_CONFIG_PATH = GENERATED_DIR / "gitlab-connect-config.json"


# ── Model ──────────────────────────────────────────────────────────────────────

class GitLabConnectConfig(BaseModel):
    gitlab_url:      str
    admin_token:     str
    worker_ip:       str
    worker_nodeport: int = 32080


# ── Helpers ────────────────────────────────────────────────────────────────────

def _api(gitlab_url: str, token: str, method: str, path: str, body=None):
    """
    Make a GitLab API call.
    Returns (response_dict_or_list, http_status_code).
    Never raises — errors are returned as ({"error": ...}, code).
    """
    url  = f"{gitlab_url}/api/v4/{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read()), e.code
        except Exception:
            return {"error": str(e)}, e.code
    except Exception as e:
        return {"error": str(e)}, 0


def _extract_ip(gitlab_url: str) -> str:
    """Extract host (IP or hostname) from a GitLab URL."""
    m = re.search(r'https?://([^/:]+)', gitlab_url)
    return m.group(1) if m else gitlab_url.lstrip("https://").lstrip("http://").split(":")[0]


# ── Configure endpoint ─────────────────────────────────────────────────────────

@router.post("/api/gitlab/connect/configure")
async def gitlab_connect_configure(cfg: GitLabConnectConfig):
    """
    Validate admin PAT and store connection params.
    Does NOT write gitlab-outputs.json — that happens in the stream.
    """
    gitlab_url = cfg.gitlab_url.rstrip("/")
    token      = cfg.admin_token.strip()

    if not gitlab_url or not token:
        return {"status": "error", "message": "gitlab_url and admin_token are required"}
    if not cfg.worker_ip:
        return {"status": "error", "message": "worker_ip is required for the OAuth callback URL"}

    # Validate PAT — must be admin
    user, code = _api(gitlab_url, token, "GET", "user")
    if code != 200:
        return {
            "status":  "error",
            "message": f"GitLab API rejected token: HTTP {code} — {user.get('message', user.get('error', ''))}"
        }
    if not user.get("is_admin"):
        return {
            "status":  "error",
            "message": (
                f"Token belongs to '{user.get('username')}' which is not an admin. "
                "An admin-level PAT is required."
            )
        }

    callback_url = f"http://{cfg.worker_ip}:{cfg.worker_nodeport}/hub/oauth_callback"

    _CONNECT_CONFIG_PATH.write_text(json.dumps({
        "gitlab_url":     gitlab_url,
        "admin_token":    token,
        "worker_ip":      cfg.worker_ip,
        "worker_nodeport": cfg.worker_nodeport,
        "callback_url":   callback_url,
    }))

    return {
        "status":       "ok",
        "username":     user.get("username"),
        "callback_url": callback_url,
        "message": (
            f"Token validated — admin: {user.get('username')} · "
            f"callback: {callback_url}"
        )
    }


# ── Stream ─────────────────────────────────────────────────────────────────────

def _connect_stream():
    """
    Create all required GitLab resources via API, write generated files,
    then configure containerd registry config on cluster nodes.

    Steps:
      1. Create / reuse  registry  group
      2. Create / reuse  notebook-images  project
      3. Rotate deploy token  (workbench-registry-token)
      4. Rotate OAuth app     (jupyterhub)
      5. Write generated/gitlab-vars.yml
      6. Write generated/gitlab-outputs.json
      7. Run ansible-k8s/configure-registry.yml  (warn-and-continue if no inventory)
    """
    if not _CONNECT_CONFIG_PATH.exists():
        yield "data: __ERROR__:no_config — run /api/gitlab/connect/configure first\n\n"
        return

    cfg          = json.loads(_CONNECT_CONFIG_PATH.read_text())
    gitlab_url   = cfg["gitlab_url"]
    token        = cfg["admin_token"]
    callback_url = cfg["callback_url"]
    gitlab_vm_ip = _extract_ip(gitlab_url)

    yield f"data: Connecting to {gitlab_url}...\n\n"

    # ── 1. registry group ────────────────────────────────────────────────────
    yield "data: [1/6] Creating registry group...\n\n"
    data, code = _api(gitlab_url, token, "POST", "groups", {
        "name": "registry", "path": "registry", "visibility": "private",
    })
    if code in (200, 201):
        yield "data: ok: registry group created\n\n"
    elif code == 400:
        yield "data: ok: registry group already exists\n\n"
    else:
        yield f"data: WARN: group create returned {code} — {data.get('message', '')}\n\n"

    data, code = _api(gitlab_url, token, "GET", "groups/registry")
    if code != 200:
        yield f"data: FAILED: cannot fetch registry group: {code}\n\n"
        yield "data: __ERROR__:group_not_found\n\n"
        return
    group_id = data["id"]
    yield f"data: ok: registry group id={group_id}\n\n"

    # ── 2. notebook-images project ───────────────────────────────────────────
    yield "data: [2/6] Creating notebook-images project...\n\n"
    data, code = _api(gitlab_url, token, "POST", "projects", {
        "name": "notebook-images",
        "path": "notebook-images",
        "namespace_id": group_id,
        "visibility": "private",
        "container_registry_access_level": "enabled",
    })
    if code in (200, 201):
        yield "data: ok: notebook-images project created\n\n"
    elif code == 400:
        yield "data: ok: notebook-images project already exists\n\n"
    else:
        yield f"data: WARN: project create returned {code}\n\n"

    data, code = _api(gitlab_url, token, "GET",
                      "projects/registry%2Fnotebook-images")
    if code != 200:
        yield f"data: FAILED: cannot fetch project info: {code}\n\n"
        yield "data: __ERROR__:project_not_found\n\n"
        return
    project_id = data["id"]
    yield f"data: ok: notebook-images project id={project_id}\n\n"

    # ── 3. deploy token ──────────────────────────────────────────────────────
    yield "data: [3/6] Rotating deploy token...\n\n"
    existing_tokens, _ = _api(gitlab_url, token, "GET",
                               f"projects/{project_id}/deploy_tokens?active=true")
    if isinstance(existing_tokens, list):
        for t in existing_tokens:
            if t.get("name") == "workbench-registry-token":
                _api(gitlab_url, token, "DELETE",
                     f"projects/{project_id}/deploy_tokens/{t['id']}")
                yield "data: ok: previous deploy token removed\n\n"

    dt, code = _api(gitlab_url, token, "POST",
                    f"projects/{project_id}/deploy_tokens", {
                        "name": "workbench-registry-token",
                        "scopes": ["read_registry", "write_registry"],
                    })
    if code not in (200, 201):
        yield f"data: FAILED: deploy token creation failed: {code} {dt}\n\n"
        yield "data: __ERROR__:deploy_token_failed\n\n"
        return
    deploy_token_user = dt["username"]
    deploy_token_pass = dt["token"]
    yield f"data: ok: deploy token created — user: {deploy_token_user}\n\n"

    # ── 4. OAuth app ─────────────────────────────────────────────────────────
    yield "data: [4/6] Rotating OAuth application...\n\n"
    apps, _ = _api(gitlab_url, token, "GET", "applications")
    if isinstance(apps, list):
        for app in apps:
            if app.get("application_name") == "jupyterhub":
                _api(gitlab_url, token, "DELETE", f"applications/{app['id']}")
                yield "data: ok: previous jupyterhub OAuth app removed\n\n"

    oauth, code = _api(gitlab_url, token, "POST", "applications", {
        "name":         "jupyterhub",
        "redirect_uri": callback_url,
        "scopes":       "api read_user openid",
        "confidential": True,
    })
    if code not in (200, 201):
        yield f"data: FAILED: OAuth app creation failed: {code} {oauth}\n\n"
        yield "data: __ERROR__:oauth_failed\n\n"
        return
    oauth_client_id     = oauth["application_id"]
    oauth_client_secret = oauth["secret"]
    yield f"data: ok: OAuth app created — client_id: {oauth_client_id}\n\n"

    # ── 5 + 6. Write generated files ─────────────────────────────────────────
    yield "data: [5/6] Writing generated files...\n\n"

    GITLAB_VARS_PATH.write_text(
        "# Generated by k8s-launcher (Attach GitLab) — do not edit manually\n\n"
        f'gitlab_vm_ip:             "{gitlab_vm_ip}"\n'
        f'gitlab_external_url:      "{gitlab_url}"\n'
        f'gitlab_registry_port:     5050\n'
        f'jupyterhub_callback_url:  "{callback_url}"\n'
        f'registry_group_name:      "registry"\n'
        f'notebook_images_project:  "notebook-images"\n'
        f'controller_outputs_path:  "{GITLAB_OUTPUTS_PATH}"\n'
    )
    yield "data: ok: gitlab-vars.yml written\n\n"

    GITLAB_OUTPUTS_PATH.write_text(json.dumps({
        "gitlab_url":                 gitlab_url,
        "gitlab_registry_host":       f"{gitlab_vm_ip}:5050",
        "registry_namespace":         "registry/notebook-images",
        "notebook_images_project_id": project_id,
        "admin_token":                token,
        "oauth_client_id":            oauth_client_id,
        "oauth_client_secret":        oauth_client_secret,
        "registry_deploy_token_user": deploy_token_user,
        "registry_deploy_token_pass": deploy_token_pass,
    }, indent=2))
    yield "data: ok: gitlab-outputs.json written\n\n"

    # Clean up temp config — credentials no longer needed in plain text
    _CONNECT_CONFIG_PATH.unlink(missing_ok=True)

    yield "data: \n\n"
    yield f"data: ✓ GitLab attached — URL: {gitlab_url} · Project ID: {project_id}\n\n"
    yield "data: \n\n"

    # ── 6. Configure registry on cluster nodes ────────────────────────────────
    yield "data: [6/6] Configuring containerd registry on cluster nodes...\n\n"

    if not INVENTORY_PATH.exists():
        yield (
            "data: [WARN] Kubernetes inventory not found — GitLab connection saved, "
            "but registry was not configured on cluster nodes yet. "
            "Run Configure → Kubernetes first, then return here and click Connect again "
            "to push the registry config to all nodes.\n\n"
        )
        yield "data: __DONE__\n\n"
        return

    registry_host = f"{gitlab_vm_ip}:5050"
    reg_cmd = [
        "ansible-playbook",
        "-i", str(INVENTORY_PATH),
        "configure-registry.yml",
        "--extra-vars", f"gitlab_registry_host={registry_host}",
    ]
    if VARS_PATH.exists():
        reg_cmd += ["--extra-vars", f"@{VARS_PATH}"]

    reg_proc = subprocess.Popen(
        reg_cmd,
        cwd=str(ANSIBLE_K8S_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    for line in iter(reg_proc.stdout.readline, ""):
        stripped = line.rstrip()
        if stripped:
            yield f"data: {stripped}\n\n"

    reg_proc.stdout.close()
    reg_proc.wait()

    if reg_proc.returncode != 0:
        yield (
            f"data: [WARN] Registry config playbook exited {reg_proc.returncode} "
            "— containerd on some nodes may need manual fix\n\n"
        )
    else:
        yield "data: Registry configured on all nodes ✓\n\n"

    yield "data: __DONE__\n\n"


@router.get("/api/gitlab/connect/stream")
async def gitlab_connect_stream():
    return StreamingResponse(
        _connect_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
