"""
k8s-launcher — entry point
"""
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import uvicorn

from core.paths import TEMPLATES_DIR
from routes import (
    bootstrap, preflight, configure,
    gitlab, k8s, longhorn, worker, reset, status,
    jupyterhub, dashboard_deploy, extensions
)

app = FastAPI(title="k8s-launcher")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/status")
async def api_status():
    return {"status": "ok", "launcher": "k8s-launcher"}

app.include_router(bootstrap.router)
app.include_router(preflight.router)
app.include_router(configure.router)
app.include_router(k8s.router)
app.include_router(longhorn.router)
app.include_router(worker.router)
app.include_router(reset.router)
app.include_router(gitlab.router)
app.include_router(jupyterhub.router)
app.include_router(dashboard_deploy.router)
app.include_router(extensions.router)
app.include_router(status.router)

if __name__ == "__main__":
    uvicorn.run("launcher:app", host="0.0.0.0", port=5000, reload=True)
