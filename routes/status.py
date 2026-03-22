"""
routes/status.py — Final output

Kubeconfig download from control plane.
"""
import os
import subprocess
import tempfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from core.paths import INVENTORY_PATH, VARS_PATH

router = APIRouter()


@router.get("/api/status/kubeconfig")
async def download_kubeconfig():
    """
    Fetch ~/.kube/config from the control plane and serve as a download.
    """
    if not INVENTORY_PATH.exists():
        raise HTTPException(
            status_code=400,
            detail="No inventory found. Run POST /api/configure first."
        )

    tmp = tempfile.mktemp(suffix=".yaml")

    result = subprocess.run(
        [
            "ansible", "control_plane",
            "-i", str(INVENTORY_PATH),
            "-m", "fetch",
            "-a", f"src=/etc/kubernetes/admin.conf dest={tmp} flat=yes",
            "--become",
            "--extra-vars", f"@{VARS_PATH}",
        ],
        capture_output=True, text=True
    )

    if result.returncode != 0 or not os.path.exists(tmp):
        raise HTTPException(
            status_code=500,
            detail=f"Could not fetch kubeconfig:\n{result.stderr}"
        )

    return FileResponse(
        path=tmp,
        filename="kubeconfig.yaml",
        media_type="application/x-yaml"
    )
