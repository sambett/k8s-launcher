"""
core/gitlab_reset.py — GitLab CE uninstall logic.

Uses a nohup shell script + log file tail so the SSE stream stays alive
during the long apt-get remove operation (~3-5 min).
Extracted from routes/reset.py to keep route files focused on HTTP concerns.
"""
import os
import subprocess
import time
from pathlib import Path

from core.paths import (
    GENERATED_DIR,
    GITLAB_INVENTORY_PATH,
    GITLAB_VARS_PATH,
    GITLAB_OUTPUTS_PATH,
)

GITLAB_BECOME_PATH = GENERATED_DIR / "gitlab-become.yml"


def _gitlab_reset_stream():
    """
    Nuclear approach: shell script + log file.
    No async, no threads, no timeouts.
    The uninstall runs via nohup. We tail the log file.
    """
    import time, os, tempfile

    yield "data: Starting GitLab reset...\n\n"

    if not GITLAB_INVENTORY_PATH.exists() or not GITLAB_BECOME_PATH.exists():
        yield "data: [gitlab] ERROR: no inventory/become file found\n\n"
        yield "data: [gitlab] Go to GitLab tab → Generate config first, then retry\n\n"
        yield "data: __DONE__\n\n"
        return

    # Write a shell script that does the uninstall
    script = "/tmp/gitlab_uninstall.sh"
    logfile = "/tmp/gitlab_uninstall.log"

    # Build ansible command
    inv  = str(GITLAB_INVENTORY_PATH)
    gv   = str(GITLAB_VARS_PATH)
    bv   = str(GITLAB_BECOME_PATH)

    with open(script, "w") as f:
        f.write(f"""#!/bin/bash
echo "[$(date)] Starting GitLab CE uninstall..." >> {logfile}
ansible gitlab \
  -i {inv} \
  -m shell \
  -a "sudo gitlab-ctl stop 2>/dev/null || true && sudo apt-get remove -y gitlab-ce 2>/dev/null || true && sudo dpkg --purge gitlab-ce 2>/dev/null || true && sudo rm -rf /etc/gitlab /var/opt/gitlab /var/log/gitlab /opt/gitlab && echo GITLAB_REMOVED" \
  --become \
  --timeout 600 \
  --extra-vars @{gv} \
  --extra-vars @{bv} >> {logfile} 2>&1
echo "[$(date)] ANSIBLE_EXIT_CODE=$?" >> {logfile}
echo "SCRIPT_DONE" >> {logfile}
""")
    os.chmod(script, 0o755)

    # Clear old log
    if os.path.exists(logfile):
        os.remove(logfile)

    # Launch script in background
    subprocess.Popen(
        ["bash", "-c", f"nohup {script} &"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True
    )

    yield "data: [gitlab] uninstall script launched in background...\n\n"
    yield "data: [gitlab] streaming log output (takes 3-5 min)...\n\n"

    # Wait for log file to appear
    waited = 0
    while not os.path.exists(logfile) and waited < 15:
        time.sleep(1)
        waited += 1

    if not os.path.exists(logfile):
        yield "data: [gitlab] ERROR: log file never appeared — check ansible manually\n\n"
        yield "data: __DONE__\n\n"
        return

    # Tail the log file until SCRIPT_DONE appears
    done = False
    last_pos = 0
    idle = 0
    while not done and idle < 700:
        time.sleep(3)
        idle += 3
        with open(logfile) as lf:
            lf.seek(last_pos)
            new_lines = lf.readlines()
            last_pos = lf.tell()
        for line in new_lines:
            line = line.strip()
            if not line:
                continue
            if "SCRIPT_DONE" in line:
                done = True
                break
            yield f"data: {line}\n\n"
        if not new_lines:
            yield f"data: [gitlab] waiting... ({idle}s)\n\n"

    # Check result
    success = False
    if os.path.exists(logfile):
        with open(logfile) as lf:
            content = lf.read()
        success = "GITLAB_REMOVED" in content
        if success:
            yield "data: [gitlab] GitLab CE fully uninstalled from VM\n\n"
        else:
            yield "data: [gitlab] uninstall completed — check log above for details\n\n"

    # Clear generated files AFTER uninstall
    yield "data: [gitlab] clearing generated credential files...\n\n"
    for path in [GITLAB_OUTPUTS_PATH, GITLAB_VARS_PATH,
                 GITLAB_BECOME_PATH, GITLAB_INVENTORY_PATH]:
        if path.exists():
            path.unlink()
    yield "data: [gitlab] credentials cleared\n\n"
    yield "data: GitLab reset complete — ready for fresh deploy.\n\n"
    yield "data: __DONE__\n\n"

