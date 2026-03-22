"""
k8s-launcher — entry point

Starts the FastAPI web server and registers all route modules.
All logic lives in routes/ and core/.
"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from core.paths import TEMPLATES_DIR
from routes import bootstrap, preflight, configure, deploy, status

app = FastAPI(title="k8s-launcher")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── Dashboard ──────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Health check ───────────────────────────────────────────────────────────────
@app.get("/api/status")
async def api_status():
    return {"status": "ok", "launcher": "k8s-launcher"}


# ── Route modules ──────────────────────────────────────────────────────────────
app.include_router(bootstrap.router)
app.include_router(preflight.router)
app.include_router(configure.router)
app.include_router(deploy.router)
app.include_router(status.router)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("launcher:app", host="0.0.0.0", port=5000, reload=True)
