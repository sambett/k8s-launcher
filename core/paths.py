from pathlib import Path

# ── Project root ───────────────────────────────────────────────────────────────
BASE_DIR             = Path(__file__).parent.parent

# ── Runtime directories ────────────────────────────────────────────────────────
GENERATED_DIR        = BASE_DIR / "generated"
TEMPLATES_DIR        = BASE_DIR / "templates"

# ── Ansible project directories ────────────────────────────────────────────────
ANSIBLE_K8S_DIR      = BASE_DIR / "ansible-k8s"
ANSIBLE_LONGHORN_DIR = BASE_DIR / "ansible-longhorn"

# ── Config files ───────────────────────────────────────────────────────────────
COMPAT_MATRIX_PATH   = BASE_DIR / "compat_matrix.json"

# ── SSH keys (ed25519, generated in Step 00 / regenerated in bootstrap) ────────
SSH_KEY_PATH         = Path.home() / ".ssh" / "id_ed25519"
SSH_PUB_KEY_PATH     = Path.home() / ".ssh" / "id_ed25519.pub"

# ── Generated file paths (created at runtime by /api/configure) ────────────────
INVENTORY_PATH       = GENERATED_DIR / "inventory.ini"
VARS_PATH            = GENERATED_DIR / "group_vars" / "all.yml"

# ── Ensure runtime directories exist on import ─────────────────────────────────
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
(GENERATED_DIR / "group_vars").mkdir(parents=True, exist_ok=True)
