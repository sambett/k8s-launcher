"""
dashboard.py — Entry point only.
Imports the app factory and starts the server.
Nothing else belongs here.
"""

import sys
from app import create_app
import app.config as config


def _check_startup_requirements():
    """
    Validate critical runtime requirements before the server starts.
    Fails loudly with a clear message rather than starting in a broken state.
    """
    if not config.DASHBOARD_PASS:
        print(
            "\n[FATAL] DASHBOARD_PASS is not set or is empty.\n"
            "The dashboard refuses to start without a password.\n\n"
            "Fix: ensure the systemd EnvironmentFile (/etc/workbench/secrets.env\n"
            "or the service unit) exports DASHBOARD_PASS with a non-empty value.\n",
            file=sys.stderr
        )
        sys.exit(1)


_check_startup_requirements()
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8888, debug=False)
