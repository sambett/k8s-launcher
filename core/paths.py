from pathlib import Path

BASE_DIR             = Path(__file__).parent.parent
GENERATED_DIR        = BASE_DIR / "generated"
TEMPLATES_DIR        = BASE_DIR / "templates"

ANSIBLE_K8S_DIR         = BASE_DIR / "ansible-k8s"
ANSIBLE_LONGHORN_DIR    = BASE_DIR / "ansible-longhorn"
ANSIBLE_GITLAB_DIR      = BASE_DIR / "ansible-gitlab"
ANSIBLE_JUPYTERHUB_DIR  = BASE_DIR / "ansible-jupyterhub"
ANSIBLE_DASHBOARD_DIR   = BASE_DIR / "ansible-dashboard"
ANSIBLE_KYVERNO_DIR    = BASE_DIR / "ansible-kyverno"
ANSIBLE_MONITORING_DIR  = BASE_DIR / "ansible-monitoring"

COMPAT_MATRIX_PATH   = BASE_DIR / "compat_matrix.json"
SSH_KEY_PATH         = Path.home() / ".ssh" / "id_ed25519"
SSH_PUB_KEY_PATH     = Path.home() / ".ssh" / "id_ed25519.pub"

INVENTORY_PATH       = GENERATED_DIR / "inventory.ini"
VARS_PATH            = GENERATED_DIR / "group_vars" / "all.yml"

GITLAB_INVENTORY_PATH   = GENERATED_DIR / "gitlab-inventory.ini"
GITLAB_VARS_PATH        = GENERATED_DIR / "gitlab-vars.yml"
GITLAB_OUTPUTS_PATH     = GENERATED_DIR / "gitlab-outputs.json"

JUPYTERHUB_VARS_PATH    = GENERATED_DIR / "jupyterhub-vars.yml"
DASHBOARD_VARS_PATH     = GENERATED_DIR / "dashboard-vars.yml"

GENERATED_DIR.mkdir(parents=True, exist_ok=True)
(GENERATED_DIR / "group_vars").mkdir(parents=True, exist_ok=True)
