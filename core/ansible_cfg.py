"""
core/ansible_cfg.py — Shared helper for writing ansible.cfg to all Ansible projects.

WHY THIS FILE EXISTS:
  Ansible uses the system OpenSSH binary (/usr/bin/ssh) under the hood.
  OpenSSH will interactively prompt "Are you sure you want to continue
  connecting (yes/no)?" for any host whose fingerprint is not in known_hosts.
  This prompt hangs Ansible forever because there is no TTY to type into.

  We solve this in TWO layers (belt + suspenders):
    Layer 1 — known_hosts population (done in bootstrap/configure/worker/gitlab)
    Layer 2 — ansible.cfg with StrictHostKeyChecking=no (done HERE)

  Layer 2 is the safety net. Even if a node was rebuilt and its fingerprint
  changed, Ansible will still connect instead of hanging.

HOW TO USE:
  from core.ansible_cfg import write_ansible_cfgs
  write_ansible_cfgs()

  Call this from any route that bootstraps a node — bootstrap, gitlab, worker.
  configure.py also calls it as a second pass.
"""

from pathlib import Path


# ── The content written to every ansible.cfg ──────────────────────────────────
#
# [defaults] section:
#   host_key_checking = False
#     → Disables Ansible's own Python-level known_hosts check.
#       This is the first gate — before Ansible even calls /usr/bin/ssh.
#
#   stdout_callback = yaml
#     → Makes Ansible output human-readable YAML format in the deploy log.
#
#   timeout = 30
#     → SSH connection timeout in seconds. Prevents hanging on unreachable nodes.
#
# [ssh_connection] section:
#   ssh_args = -o StrictHostKeyChecking=no
#     → Passed directly to the /usr/bin/ssh binary Ansible invokes.
#       This is the second gate — even if Ansible's own check is bypassed,
#       the SSH process itself will not prompt for unknown fingerprints.
#
#   -o ControlMaster=auto -o ControlPersist=60s
#     → SSH connection multiplexing. Reuses the same TCP connection for
#       multiple Ansible tasks instead of opening a new connection each time.
#       Makes playbook runs significantly faster on clusters with many tasks.
#
#   pipelining = True
#     → Sends multiple commands over a single SSH connection instead of
#       opening a new SSH session per task. Further speeds up playbook runs.
#       Requires requiretty to be disabled in sudoers — which our NOPASSWD
#       sudoers drop-in already handles.

_ANSIBLE_CFG_CONTENT = """\
[defaults]
host_key_checking = False
stdout_callback   = yaml
timeout           = 30

[ssh_connection]
ssh_args   = -o ControlMaster=auto -o ControlPersist=60s -o StrictHostKeyChecking=no
pipelining = True
"""

# ── List of every Ansible project directory in this repo ──────────────────────
# Each project runs from its own directory, so each needs its own ansible.cfg.
# Ansible looks for ansible.cfg in the current working directory first —
# which is the project directory when ansible-playbook is invoked by the launcher.
# Projects that don't exist on disk are silently skipped (no error).

_ANSIBLE_PROJECT_DIRS = [
    Path("~/k8s-launcher/ansible-k8s").expanduser(),
    Path("~/k8s-launcher/ansible-longhorn").expanduser(),
    Path("~/k8s-launcher/ansible-monitoring").expanduser(),
    Path("~/k8s-launcher/ansible-dashboard").expanduser(),
    Path("~/k8s-launcher/ansible-gitlab").expanduser(),
    Path("~/k8s-launcher/ansible-jupyterhub").expanduser(),
    Path("~/k8s-launcher/ansible-kyverno").expanduser(),
    Path("~/k8s-launcher/ansible-workers").expanduser(),
    Path("~/k8s-launcher/ansible-reset").expanduser(),
]


def write_ansible_cfgs() -> list:
    """
    Write ansible.cfg to every Ansible project directory that exists on disk.

    IMPORTANT: This function ALWAYS overwrites existing ansible.cfg files.
    We never skip — because an existing file might be missing StrictHostKeyChecking=no
    and we have no way to know that without reading and parsing it.
    Always overwriting guarantees correctness on every run.

    Returns:
        List of file paths that were written (for logging/debugging).
    """
    written = []
    for project_dir in _ANSIBLE_PROJECT_DIRS:
        # Skip directories that don't exist — not all projects are present
        # on every deployment (e.g. ansible-monitoring may not be installed yet)
        if not project_dir.exists():
            continue

        cfg_path = project_dir / "ansible.cfg"
        cfg_path.write_text(_ANSIBLE_CFG_CONTENT)
        written.append(str(cfg_path))

    return written
