"""
routes/configure.py — Phase 3 of the setup flow.

Does four things when the user clicks "Generate configuration":

1. Writes generated/inventory.ini — the Ansible host list
2. Writes generated/group_vars/all.yml — all cluster variables including
   pause_image, which is derived live from the control plane via kubeadm
   so it is always correct regardless of Kubernetes version
3. Wires cplane → workers passwordless SSH
   The control plane needs to SSH into its own workers for kubectl delegate_to
   tasks. Since configure already knows which node is the cplane and which are
   workers, this is the natural place to set that up. No passwords needed —
   bootstrap already pushed the controller key to all nodes, so we connect
   using the private key.
4. Writes ansible.cfg to every Ansible project (via shared helper)
   Always overwrites so StrictHostKeyChecking=no is guaranteed on every run.

All generated files go to generated/ which is git-ignored.
No Ansible project files are ever modified except ansible.cfg.
"""
import json
import subprocess
from pathlib import Path
from typing import List

import paramiko
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.paths import (
    SSH_KEY_PATH,
    SSH_PUB_KEY_PATH,
    INVENTORY_PATH,
    VARS_PATH,
    COMPAT_MATRIX_PATH,
    BOOTSTRAP_NODES_PATH,
)
from core.ssh import get_client_with_key, run_command

# Shared helper — writes ansible.cfg with StrictHostKeyChecking=no to every
# Ansible project directory. Always overwrites. Called here AND from bootstrap
# so the guarantee holds regardless of which tab the user visits first.
from core.ansible_cfg import write_ansible_cfgs

router = APIRouter()


def _load_bootstrap_nodes() -> list:
    """
    Read the saved bootstrap registry. Configure uses this as the allowed
    machine set so cluster config cannot drift from bootstrapped nodes.
    """
    if not BOOTSTRAP_NODES_PATH.exists():
        return []

    try:
        data = json.loads(BOOTSTRAP_NODES_PATH.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _validate_config_nodes(config: "ClusterConfig") -> None:
    """
    Enforce the same machine-selection contract on the backend that the UI
    enforces in the browser.
    """
    saved_nodes = _load_bootstrap_nodes()
    if not saved_nodes:
        raise HTTPException(
            status_code=400,
            detail="No bootstrapped machines found. Run Bootstrap SSH first."
        )

    saved_by_ip = {node.get("ip"): node for node in saved_nodes if node.get("ip")}
    requested = [config.control_plane] + config.workers

    seen_hostnames = set()
    seen_ips = set()

    for node in requested:
        if node.ip not in saved_by_ip:
            raise HTTPException(
                status_code=400,
                detail=f"{node.hostname} ({node.ip}) is not in the bootstrapped machine list."
            )

        saved = saved_by_ip[node.ip]
        if node.hostname != saved.get("hostname") or node.ssh_user != saved.get("ssh_user"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{node.ip} does not match the saved bootstrap record "
                    f"({saved.get('hostname')} / {saved.get('ssh_user')})."
                )
            )

        if node.hostname in seen_hostnames:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate hostname in cluster configuration: {node.hostname}"
            )
        if node.ip in seen_ips:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate IP in cluster configuration: {node.ip}"
            )

        seen_hostnames.add(node.hostname)
        seen_ips.add(node.ip)


# ── Models ─────────────────────────────────────────────────────────────────────

class WorkerNode(BaseModel):
    ip: str
    hostname: str
    ssh_user: str


class ControlPlane(BaseModel):
    ip: str
    hostname: str
    ssh_user: str


class ClusterConfig(BaseModel):
    control_plane: ControlPlane
    workers: List[WorkerNode]
    kubernetes_version: str = "1.30.5"
    containerd_version: str = "1.7.22"
    calico_version: str     = "v3.28.2"
    longhorn_version: str   = "1.7.2"
    pod_cidr: str           = "192.168.0.0/16"
    service_cidr: str       = "10.96.0.0/12"
    cluster_name: str       = "k8s-cluster"


# ── Replica logic ──────────────────────────────────────────────────────────────

def _replica_settings(worker_count: int) -> tuple:
    """
    Choose Longhorn replica count based on how many workers exist.
    1 worker  → 1 replica (can't replicate across 0 extra nodes)
    2 workers → 2 replicas (one per worker, no soft anti-affinity needed)
    3+ workers → 3 replicas (standard production setting)
    """
    if worker_count == 1:
        return 1, "true"
    elif worker_count == 2:
        return 2, "false"
    else:
        return 3, "false"


# ── Pause image derivation ─────────────────────────────────────────────────────

def _get_pause_image(cp: ControlPlane, kubernetes_version: str) -> str:
    """
    Derive the correct pause (sandbox) container image for the given
    Kubernetes version by asking the control plane directly.

    Why this must not be hardcoded:
      The pause image version is tied to the Kubernetes minor version.
      Hardcoding it means the value silently breaks when kubernetes_version
      changes — the containerd role would pin the wrong sandbox image on
      every new worker, causing spurious pod restarts under node pressure.

    How it works:
      SSH into the control plane and run:
        kubeadm config images list --kubernetes-version <version>
      This is the official kubeadm command for listing required images.
      We filter for the pause image and return it exactly as kubeadm reports it.
      kubeadm is always present on the control plane — it was used to init
      the cluster and is version-held at the same version as the cluster.

    Fallback:
      If the control plane is unreachable at configure time (e.g. configure
      is re-run before the cluster is up), we derive the value from the
      official Kubernetes release notes mapping:
        1.28.x → pause:3.9
        1.29.x → pause:3.9
        1.30.x → pause:3.9
        1.31.x → pause:3.10
        1.32.x → pause:3.10
      This mapping is stable — the pause image version only changes on
      Kubernetes minor version boundaries, not patch releases.
      The fallback is a safety net, not the primary path.
    """
    # ── Primary: ask kubeadm on the control plane ──────────────────────────────
    # BUG FIX: get_client_with_key(ip, user) — do NOT pass SSH_KEY_PATH as a
    # third argument. The function signature is (ip, ssh_user, timeout=10).
    # The key path is already hardcoded inside get_client_with_key via
    # key_filename=str(SSH_KEY_PATH). Passing it as positional arg 3 silently
    # assigns it to `timeout`, which causes Paramiko to raise TypeError on
    # sock.settimeout() and drops into the except block every time.
    client = None
    try:
        client = get_client_with_key(cp.ip, cp.ssh_user)
        out, _, rc = run_command(
            client,
            f"kubeadm config images list --kubernetes-version {kubernetes_version} "
            f"2>/dev/null | grep pause"
        )
        if rc == 0 and out.strip():
            # out may contain Ansible ad-hoc noise — take the last non-empty line
            # which will be the actual image reference e.g. registry.k8s.io/pause:3.9
            image = out.strip().splitlines()[-1].strip()
            if image.startswith("registry.k8s.io/pause"):
                return image
    except Exception:
        # Control plane unreachable — fall through to version-derived fallback
        pass
    finally:
        if client:
            client.close()

    # ── Fallback: derive from kubernetes minor version ─────────────────────────
    # Extract minor version integer from "1.30.5" → 30
    try:
        minor = int(kubernetes_version.split(".")[1])
    except (IndexError, ValueError):
        minor = 30  # safe default if version string is malformed

    # pause:3.9  → Kubernetes 1.28, 1.29, 1.30
    # pause:3.10 → Kubernetes 1.31, 1.32+
    # Source: https://github.com/kubernetes/kubernetes/blob/master/build/pause/CHANGELOG.md
    if minor >= 31:
        return "registry.k8s.io/pause:3.10"
    else:
        return "registry.k8s.io/pause:3.9"


# ── File generators ────────────────────────────────────────────────────────────

def _make_inventory(config: ClusterConfig) -> str:
    """
    Build inventory.ini content from user-supplied node details.
    The ansible_ssh_private_key_file points to the key bootstrap generated,
    so Ansible never needs a password.
    """
    cp = config.control_plane
    lines = [
        "[control_plane]",
        f"{cp.hostname} ansible_host={cp.ip} ansible_user={cp.ssh_user}",
        "",
        "[workers]",
    ]
    for w in config.workers:
        lines.append(f"{w.hostname} ansible_host={w.ip} ansible_user={w.ssh_user}")
    lines += [
        "",
        "[all:vars]",
        "ansible_python_interpreter=/usr/bin/python3",
        f"ansible_ssh_private_key_file={SSH_KEY_PATH}",
    ]
    return "\n".join(lines) + "\n"


def _make_group_vars(config: ClusterConfig, pause_image: str) -> str:
    """
    Build group_vars/all.yml content from user-supplied cluster config.
    This file is the single source of truth for all Ansible roles — versions,
    network CIDRs, Longhorn settings, node hostnames and IPs.

    pause_image is passed in as a parameter rather than derived here because
    deriving it requires an SSH call to the control plane, which belongs in
    the configure() endpoint — not inside a pure string-building function.
    """
    cp = config.control_plane
    replica_count, soft_anti = _replica_settings(len(config.workers))

    hosts = [f"  - {{ name: {cp.hostname}, ip: \"{cp.ip}\" }}"]
    for w in config.workers:
        hosts.append(f"  - {{ name: {w.hostname}, ip: \"{w.ip}\" }}")

    calico = config.calico_version
    if not calico.startswith("v"):
        calico = "v" + calico

    return f"""# Generated by k8s-launcher — do not edit manually

# ── Kubernetes ─────────────────────────────────────────────────────────────────
kubernetes_version: "{config.kubernetes_version}"
containerd_version: "{config.containerd_version}"
calico_version:     "{calico}"

# ── Control plane ──────────────────────────────────────────────────────────────
cp_hostname: "{cp.hostname}"
cp_ip:       "{cp.ip}"

# ── Networking ─────────────────────────────────────────────────────────────────
pod_cidr:     "{config.pod_cidr}"
service_cidr: "{config.service_cidr}"
cluster_name: "{config.cluster_name}"

# ── Hosts entries ──────────────────────────────────────────────────────────────
cluster_hosts:
{chr(10).join(hosts)}

# ── Artifact directory ─────────────────────────────────────────────────────────
artifacts_dir: "/home/{cp.ssh_user}/cluster-artifacts"

# ── Longhorn ───────────────────────────────────────────────────────────────────
longhorn_version:                    "{config.longhorn_version}"
longhorn_namespace:                  "longhorn-system"
longhorn_replica_count:              {replica_count}
longhorn_replica_soft_anti_affinity: "{soft_anti}"
longhorn_data_path:                  "/var/lib/longhorn"
longhorn_storageclass_name:          "longhorn-jupyterhomes"
longhorn_storageclass_default:       "true"
longhorn_reclaim_policy:             "Retain"
longhorn_over_provisioning_pct:      150
longhorn_min_available_pct:          25
longhorn_node_drain_policy:          "block-if-contains-last-replica"
longhorn_artifacts_dir:              "/home/{cp.ssh_user}/cluster-artifacts/longhorn"

# ── Container runtime ──────────────────────────────────────────────────────────
# pause_image is derived live from the control plane via:
#   kubeadm config images list --kubernetes-version <version> | grep pause
# It is written here so the containerd role always pins the correct sandbox
# image on every new worker, regardless of Kubernetes version.
# Never hardcode this value — it changes with Kubernetes minor versions.
pause_image: "{pause_image}"
"""


# ── cplane → workers SSH wiring ────────────────────────────────────────────────

def _wire_cplane_to_workers(cp: ControlPlane, workers: List[WorkerNode]) -> dict:
    """
    Give the control plane passwordless SSH access to all worker nodes.

    Why this is needed:
      Ansible playbooks use 'delegate_to: control_plane' for kubectl commands
      (ansiblectl has no kubeconfig). When Ansible runs a delegated task it
      opens a NEW SSH connection from ansiblectl → cplane, then cplane may
      need to SSH to workers for certain operations. Without this wiring,
      those connections prompt for a fingerprint or password and hang.

    How it works (all using the controller's private key — no passwords):
      Step 1 — SSH into cplane, generate its own ed25519 keypair if missing
      Step 2 — Read cplane's public key
      Step 3 — For each worker: push cplane's pubkey into authorized_keys
      Step 4 — On cplane: run ssh-keyscan for each worker IP
                → populate cplane's known_hosts so no fingerprint prompt fires

    BUG FIX: all get_client_with_key() calls use only (ip, user). The third
    parameter is timeout, not key path — the key is already wired inside the
    function. Passing str(SSH_KEY_PATH) as arg 3 caused TypeError in Paramiko's
    sock.settimeout() on every call, silently breaking all SSH wiring here.
    """
    if not workers:
        return {"status": "skipped", "message": "No workers to wire"}

    report = {"cplane_keygen": None, "workers": []}

    # ── Steps 1 & 2 — generate cplane keypair if missing, read pubkey ─────────
    cp_client = None
    try:
        cp_client = get_client_with_key(cp.ip, cp.ssh_user)

        _, _, rc = run_command(cp_client, "test -f ~/.ssh/id_ed25519.pub")
        if rc != 0:
            # No keypair yet — generate one silently (-q) with no passphrase
            _, stderr, rc = run_command(
                cp_client,
                "ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N '' -q"
            )
            if rc != 0:
                return {
                    "status":  "error",
                    "message": f"cplane keypair generation failed: {stderr}"
                }
            report["cplane_keygen"] = "generated"
        else:
            report["cplane_keygen"] = "already exists"

        cplane_pubkey, _, rc = run_command(cp_client, "cat ~/.ssh/id_ed25519.pub")
        if rc != 0 or not cplane_pubkey.strip():
            return {"status": "error", "message": "Could not read cplane public key"}
        cplane_pubkey = cplane_pubkey.strip()

    except Exception as exc:
        return {"status": "error", "message": f"Cannot reach cplane: {exc}"}
    finally:
        if cp_client:
            cp_client.close()

    # ── Step 3 — push cplane pubkey to each worker ────────────────────────────
    for worker in workers:
        w_client = None
        try:
            # Connect to worker using the CONTROLLER's key (bootstrap already pushed it)
            w_client = get_client_with_key(worker.ip, worker.ssh_user)

            cmds = [
                "mkdir -p ~/.ssh",
                "chmod 700 ~/.ssh",
                f"echo '{cplane_pubkey}' >> ~/.ssh/authorized_keys",
                # Deduplicate so running configure multiple times is safe
                "sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys",
                "chmod 600 ~/.ssh/authorized_keys",
            ]
            for cmd in cmds:
                _, stderr, exit_code = run_command(w_client, cmd)
                if exit_code != 0:
                    report["workers"].append({
                        "ip":     worker.ip,
                        "status": "error",
                        "message": f"Command failed: {cmd} — {stderr}"
                    })
                    break
            else:
                report["workers"].append({
                    "ip":     worker.ip,
                    "status": "ok",
                    "message": "cplane pubkey pushed"
                })

        except Exception as exc:
            report["workers"].append({
                "ip":     worker.ip,
                "status": "error",
                "message": f"Cannot reach worker: {exc}"
            })
        finally:
            if w_client:
                w_client.close()

    # ── Step 4 — populate cplane known_hosts for all workers ──────────────────
    # ssh-keyscan runs ON cplane (not on the controller) so cplane's own
    # known_hosts gets the worker fingerprints.
    cp_client = None
    try:
        cp_client = get_client_with_key(cp.ip, cp.ssh_user)

        for worker in workers:
            scan_out, _, _ = run_command(
                cp_client,
                f"ssh-keyscan -H -T 5 {worker.ip} 2>/dev/null"
            )
            if scan_out.strip():
                run_command(
                    cp_client,
                    f"touch ~/.ssh/known_hosts && "
                    f"echo '{scan_out.strip()}' >> ~/.ssh/known_hosts"
                )

        # Deduplicate cplane known_hosts — safe to run multiple times
        run_command(cp_client, "sort -u ~/.ssh/known_hosts -o ~/.ssh/known_hosts")

    except Exception as exc:
        # Non-fatal — key push already done, this is belt-and-suspenders
        report["known_hosts_warning"] = f"cplane known_hosts population failed: {exc}"
    finally:
        if cp_client:
            cp_client.close()

    failed = [w for w in report["workers"] if w["status"] == "error"]
    report["status"] = "error" if failed else "ok"
    return report


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/api/configure")
async def configure(config: ClusterConfig):
    """
    Generate inventory + group_vars, wire cplane→workers SSH, write ansible.cfg.
    All steps happen on every click of "Generate configuration".

    pause_image is derived here — not in _make_group_vars — because it requires
    an SSH call to the control plane. _make_group_vars is a pure string builder
    and must not have side effects. The derived value is passed in as a parameter.

    BUG FIX: the top-level response status is now "error" when the SSH wiring
    step fails. Previously it always returned "ok", hiding the failure from the
    UI and from the operator. Files are still written on SSH wiring failure —
    inventory and group_vars are valid — but the operator must know to re-run
    or investigate before relying on cplane→worker SSH.
    """
    _validate_config_nodes(config)

    # 1. Derive pause_image from the control plane before writing group_vars.
    pause_image = _get_pause_image(config.control_plane, config.kubernetes_version)

    # 2. Write generated files — inventory and group_vars
    INVENTORY_PATH.write_text(_make_inventory(config))
    VARS_PATH.write_text(_make_group_vars(config, pause_image))
    replica_count, soft_anti = _replica_settings(len(config.workers))

    # 3. Write ansible.cfg to all Ansible projects (always overwrite)
    cfgs_written = write_ansible_cfgs()

    # 4. Wire cplane → workers passwordless SSH
    wire_report = _wire_cplane_to_workers(config.control_plane, config.workers)

    # Surface SSH wiring failures to the caller — do not claim success when
    # a critical trust path was not established.
    top_status = "error" if wire_report.get("status") == "error" else "ok"

    return {
        "status": top_status,
        "files": {
            "inventory":  str(INVENTORY_PATH),
            "group_vars": str(VARS_PATH)
        },
        "derived": {
            "worker_count":                        len(config.workers),
            "longhorn_replica_count":              replica_count,
            "longhorn_replica_soft_anti_affinity": soft_anti,
            "pause_image":                         pause_image,
        },
        "ansible_cfgs_written": cfgs_written,
        "cplane_to_workers":    wire_report
    }


@router.get("/api/configure/preview")
async def preview_files():
    """Return the current generated inventory and group_vars for UI display."""
    if not INVENTORY_PATH.exists() or not VARS_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail="No generated files found. Run POST /api/configure first."
        )
    return {
        "inventory":  INVENTORY_PATH.read_text(),
        "group_vars": VARS_PATH.read_text()
    }


@router.get("/api/compat-matrix")
async def get_compat_matrix():
    """Return the compatibility matrix for version validation in the UI."""
    if not COMPAT_MATRIX_PATH.exists():
        raise HTTPException(status_code=404, detail="compat_matrix.json not found")
    return json.loads(COMPAT_MATRIX_PATH.read_text())
