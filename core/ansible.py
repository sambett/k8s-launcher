"""
core/ansible.py — Shared Ansible helpers used by all deploy routes.
"""
import subprocess
from pathlib import Path

from core.paths import INVENTORY_PATH, VARS_PATH


def ansible_stream(playbook_dir: Path):
    if not INVENTORY_PATH.exists():
        yield "data: __ERROR__:no_inventory\n\n"
        return

    process = subprocess.Popen(
        ["ansible-playbook", "-i", str(INVENTORY_PATH), "site.yml", "--extra-vars", f"@{VARS_PATH}"],
        cwd=str(playbook_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    for line in iter(process.stdout.readline, ""):
        stripped = line.rstrip()
        if stripped:
            yield f"data: {stripped}\n\n"

    process.stdout.close()
    process.wait()

    if process.returncode == 0:
        yield "data: __DONE__\n\n"
    else:
        yield f"data: __ERROR__:{process.returncode}\n\n"


def read_cp_ip() -> str:
    """Read control plane IP from generated vars file. Single source of truth."""
    from core.paths import VARS_PATH
    if not VARS_PATH.exists():
        return ""
    for line in VARS_PATH.read_text().splitlines():
        if line.strip().startswith("cp_ip:"):
            return line.split(":", 1)[1].strip().strip('"')
    return ""


def run_on_cp(cmd: str) -> tuple:
    result = subprocess.run(
        ["ansible", "control_plane", "-i", str(INVENTORY_PATH), "-m", "shell", "-a", cmd, "--extra-vars", f"@{VARS_PATH}"],
        capture_output=True, text=True
    )
    return result.stdout + result.stderr, result.returncode
