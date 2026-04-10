"""
config.py — All configuration values in one place.
Reads env vars and registry.conf. No business logic here.
Changes only when deployment configuration changes.
"""

import os
import yaml

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR           = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_CONF_PATH = os.path.join(BASE_DIR, "registry.conf")
SECRETS_ENV_PATH   = "/etc/workbench/secrets.env"

# ── Kubernetes ─────────────────────────────────────────────────────────────────
CONFIGMAP_NAME = "jupyterhub-profiles"
CONFIGMAP_NS   = "jhub"

# ── Dashboard auth ─────────────────────────────────────────────────────────────
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "")

# ── Registry conf helpers ──────────────────────────────────────────────────────
def load_registry_conf():
    try:
        with open(REGISTRY_CONF_PATH) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise RuntimeError(
            f"registry.conf not found at {REGISTRY_CONF_PATH}. "
            "Run the Ansible deployment playbook first, or copy registry.conf.example "
            "and fill in the correct values."
        )
    except Exception as e:
        raise RuntimeError(
            f"registry.conf could not be read ({REGISTRY_CONF_PATH}): {e}"
        )

    if not isinstance(data, dict) or "registry" not in data:
        raise RuntimeError(
            f"registry.conf is malformed — expected a top-level 'registry:' key. "
            f"Check {REGISTRY_CONF_PATH} against registry.conf.example."
        )

    return data["registry"]

def gitlab_url():
    return load_registry_conf().get("gitlab_url", "http://10.110.188.61")

def registry_project_id():
    return load_registry_conf().get("project_id", 2)
