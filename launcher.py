import os
import subprocess
import socket
import concurrent.futures
import tempfile
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uvicorn
import paramiko

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR             = Path(__file__).parent
GENERATED_DIR        = BASE_DIR / "generated"
TEMPLATES_DIR        = BASE_DIR / "templates"
ANSIBLE_K8S_DIR      = BASE_DIR / "ansible-k8s"
ANSIBLE_LONGHORN_DIR = BASE_DIR / "ansible-longhorn"
COMPAT_MATRIX_PATH   = BASE_DIR / "compat_matrix.json"

# ed25519 key — generated in Step 00
SSH_KEY_PATH     = Path.home() / ".ssh" / "id_ed25519"
SSH_PUB_KEY_PATH = Path.home() / ".ssh" / "id_ed25519.pub"

# ensure generated dirs exist on startup
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
(GENERATED_DIR / "group_vars").mkdir(parents=True, exist_ok=True)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="k8s-launcher")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── Dashboard ──────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Status ─────────────────────────────────────────────────────────────────────
@app.get("/api/status")
async def status():
    return {"status": "ok", "launcher": "k8s-launcher"}


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("launcher:app", host="0.0.0.0", port=5000, reload=True)
