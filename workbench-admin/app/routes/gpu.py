import json
import os
import shutil
import subprocess
import tempfile

from flask import Blueprint, jsonify, request
from app.auth import require_auth
import app.config as config

bp = Blueprint("gpu", __name__)

_KUBECTL       = shutil.which("kubectl") or "/usr/local/bin/kubectl"
MATRIX_CM_NAME = "gpu-compat-matrix"
MATRIX_CM_NS   = config.CONFIGMAP_NS


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


@bp.route("/api/gpu/inventory")
@require_auth
def api_gpu_inventory():
    r = _run([_KUBECTL, "get", "nodes", "-o", "json"])
    if r.returncode != 0:
        return jsonify({"nodes": [], "models": [], "error": r.stderr}), 500
    try:
        data = json.loads(r.stdout)
    except Exception as e:
        return jsonify({"nodes": [], "models": [], "error": str(e)}), 500

    nodes       = []
    models_seen = {}

    for item in data.get("items", []):
        meta   = item.get("metadata", {})
        labels = meta.get("labels", {})
        status = item.get("status", {})

        if "node-role.kubernetes.io/control-plane" in labels:
            continue

        name        = meta.get("name", "")
        node_ready  = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in status.get("conditions", [])
        )
        gpu_product = labels.get("nvidia.com/gpu.product", "")
        gpu_memory  = labels.get("nvidia.com/gpu.memory", "")
        cuda_major  = labels.get("nvidia.com/cuda.runtime.major", "")
        cuda_minor  = labels.get("nvidia.com/cuda.runtime.minor", "")
        gpu_present = labels.get("nvidia.com/gpu.present", "")
        allocatable = status.get("allocatable", {}).get("nvidia.com/gpu", "0") or "0"

        cuda_ceiling = ""
        if cuda_major and cuda_minor:
            cuda_ceiling = f"{cuda_major}.{cuda_minor}"
        elif cuda_major:
            cuda_ceiling = cuda_major

        nodes.append({
            "name":         name,
            "status":       "Ready" if node_ready else "NotReady",
            "gfd_active":   bool(gpu_present or gpu_product),
            "gpu_product":  gpu_product,
            "gpu_memory":   gpu_memory,
            "cuda_ceiling": cuda_ceiling,
            "allocatable":  allocatable,
        })

        if gpu_product and gpu_product not in models_seen:
            models_seen[gpu_product] = {
                "gpu_model":    gpu_product,
                "cuda_ceiling": cuda_ceiling,
                "gpu_memory":   gpu_memory,
                "example_node": name,
                # max_count is not available from GFD labels — it comes from
                # the compatibility matrix saved by the admin. We set a safe
                # default of 1 here so the inventory response is always usable
                # even before the admin has saved the matrix. The profile form
                # reads the full matrix via /api/gpu/matrix which has the real
                # admin-defined value.
                "max_count": 1,
            }

    return jsonify({"nodes": nodes, "models": list(models_seen.values())})


@bp.route("/api/gpu/matrix")
@require_auth
def api_get_matrix():
    r = _run([
        _KUBECTL, "get", "configmap", MATRIX_CM_NAME,
        "-n", MATRIX_CM_NS, "-o", "jsonpath={.data.matrix}"
    ])
    if r.returncode != 0 or not r.stdout.strip():
        return jsonify({"matrix": []})
    try:
        matrix = json.loads(r.stdout.strip())
        # Back-fill max_count for entries saved before this field existed.
        # Ensures the profile form always has a usable count ceiling even
        # if the admin has not yet re-saved the matrix after the upgrade.
        for entry in matrix:
            entry.setdefault("max_count", 1)
        return jsonify({"matrix": matrix})
    except Exception:
        return jsonify({"matrix": []})


@bp.route("/api/gpu/matrix", methods=["POST"])
@require_auth
def api_save_matrix():
    data   = request.json or {}
    matrix = data.get("matrix", [])

    for entry in matrix:
        if not entry.get("gpu_model"):
            return jsonify({"success": False, "error": "Each entry needs a gpu_model"}), 400
        try:
            if int(entry.get("min_cpu", 0)) < 1:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"success": False,
                            "error": f"min_cpu must be >= 1 for {entry.get('gpu_model')}"}), 400
        if not str(entry.get("min_ram", "")).strip():
            return jsonify({"success": False,
                            "error": f"min_ram is required for {entry.get('gpu_model')}"}), 400

        # max_count — how many GPUs of this model can a single pod request.
        # Optional field. Defaults to 1 if absent. Must be an integer >= 1.
        # Drives the GPU count dropdown in the new profile form.
        # Prevents creating profiles that can never be scheduled on this hardware.
        raw_max = entry.get("max_count", 1)
        try:
            max_count = int(raw_max)
            if max_count < 1:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({
                "success": False,
                "error": (
                    f"max_count must be an integer >= 1 "
                    f"for {entry.get('gpu_model')}. Got: {raw_max!r}"
                )
            }), 400
        entry["max_count"] = max_count

    json_str = json.dumps(matrix, indent=2)
    check    = _run([_KUBECTL, "get", "configmap", MATRIX_CM_NAME, "-n", MATRIX_CM_NS])

    if check.returncode != 0:
        r = _run([
            _KUBECTL, "create", "configmap", MATRIX_CM_NAME,
            "-n", MATRIX_CM_NS,
            "--from-literal", f"matrix={json_str}"
        ])
    else:
        patch = {"data": {"matrix": json_str}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(patch, f)
            pf = f.name
        try:
            r = _run([
                _KUBECTL, "patch", "configmap", MATRIX_CM_NAME,
                "-n", MATRIX_CM_NS, "--type=merge", "--patch-file", pf
            ])
        finally:
            os.unlink(pf)

    if r.returncode == 0:
        return jsonify({"success": True, "message": f"{len(matrix)} GPU type(s) saved to cluster."})
    return jsonify({"success": False, "error": r.stderr or r.stdout}), 500
