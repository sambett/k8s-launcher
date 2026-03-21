# ansible-longhorn — Usage Guide

## Table of Contents
1. [What This Repo Does](#1-what-this-repo-does)
2. [Prerequisites](#2-prerequisites)
3. [Repo Structure](#3-repo-structure)
4. [How to Deploy on Your Cluster](#4-how-to-deploy-on-your-cluster)
5. [How to Adapt for a Different Cluster](#5-how-to-adapt-for-a-different-cluster)
6. [Variables Reference](#6-variables-reference)
7. [How to Reset and Redeploy](#7-how-to-reset-and-redeploy)
8. [Verifying the Installation Manually](#8-verifying-the-installation-manually)
9. [Backup the Project Files](#9-backup-the-project-files)

---

## 1. What This Repo Does

Deploys Longhorn 1.7.2 distributed block storage on an existing Kubernetes cluster.

```bash
ansible-playbook -i inventory/hosts.yml site.yml
```

It installs and configures:
- All OS-level node prerequisites (iscsi_tcp, cryptsetup, multipathd, iscsid)
- Helm v3 on the control plane
- Longhorn 1.7.2 via Helm with version-pinned, reproducible values
- A custom StorageClass (`longhorn-jupyterhomes`) as the cluster default
- End-to-end validation: PVC provisioned, Bound, and cleaned up

**This repo is independent from `ansible-k8s/`. Run `ansible-k8s` first to create
the cluster, then run this repo to add storage.**

---

## 2. Prerequisites

Before running this playbook, confirm:

| Requirement | How to verify |
|---|---|
| Kubernetes cluster is running | `kubectl get nodes` — all nodes Ready |
| Ansible installed on control plane | `ansible --version` |
| Passwordless sudo on all nodes | `sudo whoami` returns `root` with no prompt |
| SSH key-based auth from CP to workers | `ssh <worker-user>@<worker-ip>` with no password |
| Internet access on all nodes | `curl -I https://charts.longhorn.io` returns 200 |

`open-iscsi` and `nfs-common` are expected to already be installed on all nodes.
If you used `ansible-k8s` to build your cluster, they are already there.
If not, install them manually before running this playbook:

```bash
sudo apt install -y open-iscsi nfs-common
sudo systemctl enable --now iscsid
```

---

## 3. Repo Structure

```
ansible-longhorn/
├── site.yml                              ← master playbook — run this
├── inventory/
│   └── hosts.yml                         ← YOUR nodes go here
├── group_vars/
│   └── all.yml                           ← ALL Longhorn settings go here
└── roles/
    ├── longhorn_prereqs/tasks/main.yml   ← OS prereqs on ALL nodes
    ├── helm/tasks/main.yml               ← Helm v3 install on CP
    └── longhorn/
        ├── tasks/main.yml                ← full Longhorn deployment
        └── templates/
            ├── longhorn-values.yaml.j2   ← Helm values (rendered at deploy time)
            ├── storageclass-jupyterhomes.yaml.j2  ← custom StorageClass
            └── test-pvc.yaml.j2          ← validation PVC
```

**The two files you always edit when changing clusters:**

| File | What to change |
|---|---|
| `inventory/hosts.yml` | Hostnames, IPs, SSH users of your nodes |
| `group_vars/all.yml` | Longhorn version, replica count, StorageClass name |

---

## 4. How to Deploy on Your Cluster

### Step 1 — Copy this repo to your control plane

From your local machine:

```bash
scp -rO ansible-longhorn/ <cp-user>@<cp-ip>:~/
```

### Step 2 — Edit inventory and variables

See section 5 below.

### Step 3 — Verify Ansible can reach all nodes

```bash
cd ~/ansible-longhorn
ansible -i inventory/hosts.yml all -m ping
```

Both nodes must return `pong`.

### Step 4 — Deploy

```bash
ansible-playbook -i inventory/hosts.yml site.yml
```

### Step 5 — Confirm success

```
PLAY RECAP
<cp-hostname>     : ok=30   changed=N   unreachable=0   failed=0
<worker-hostname> : ok=6    changed=N   unreachable=0   failed=0
```

`failed=0` on both nodes is the only acceptable result.

---

## 5. How to Adapt for a Different Cluster

### Edit `inventory/hosts.yml`

```yaml
all:
  children:
    control_plane:
      hosts:
        myControlPlane:               # ← your control plane hostname
          ansible_connection: local
          ansible_user: mycp_user     # ← SSH user on the CP

    workers:
      hosts:
        myWorker01:                   # ← worker hostname
          ansible_host: 10.0.0.11    # ← worker IP
          ansible_user: myworker_user

        myWorker02:                   # ← add more workers here
          ansible_host: 10.0.0.12
          ansible_user: myworker_user

  vars:
    ansible_python_interpreter: /usr/bin/python3
```

### Edit `group_vars/all.yml`

The minimum fields to change for a new cluster:

```yaml
# ── Replica count ─────────────────────────────────────────────────────────────
# Set to the number of schedulable worker nodes.
# 1 worker  → longhorn_replica_count: 1
# 2 workers → longhorn_replica_count: 2
longhorn_replica_count: 2

# ── Replica soft anti-affinity ────────────────────────────────────────────────
# 1 worker  → "true"   (strict mode would cause Degraded volumes)
# 2 workers → "false"  (Longhorn best-practice default)
longhorn_replica_soft_anti_affinity: "false"

# ── Artifact directory ────────────────────────────────────────────────────────
# Change the username to match your control plane user
longhorn_artifacts_dir: "/home/mycp_user/cluster-artifacts/longhorn"

# ── Control plane identity ────────────────────────────────────────────────────
cp_hostname: "mycontrolplane"   # must be lowercase, must match kubectl node name
cp_ip:       "10.0.0.10"
```

> **Important:** `cp_hostname` must match exactly what `kubectl get nodes` shows.
> Kubernetes always registers node names in lowercase regardless of your hostname.

---

## 6. Variables Reference

All variables live in `group_vars/all.yml`. Full reference:

| Variable | Default | Description |
|---|---|---|
| `longhorn_version` | `1.7.2` | Longhorn chart version to install |
| `longhorn_namespace` | `longhorn-system` | Kubernetes namespace for Longhorn |
| `longhorn_replica_count` | `1` | Replicas per volume — set to worker node count |
| `longhorn_replica_soft_anti_affinity` | `"true"` | `"false"` for 2+ workers (strict), `"true"` for 1 worker |
| `longhorn_storageclass_name` | `longhorn-jupyterhomes` | Name of the custom StorageClass |
| `longhorn_storageclass_default` | `"true"` | Makes this the cluster-wide default StorageClass |
| `longhorn_reclaim_policy` | `Retain` | `Retain` keeps data when PVC deleted. Use `Delete` for auto-cleanup |
| `longhorn_over_provisioning_pct` | `150` | Allow provisioning up to 1.5x available disk. Use `100` in production |
| `longhorn_min_available_pct` | `25` | Refuse new replicas if free disk falls below this % |
| `longhorn_node_drain_policy` | `block-if-contains-last-replica` | Prevents data loss during node drain |
| `longhorn_data_path` | `/var/lib/longhorn` | Where Longhorn stores replica data on each worker |
| `longhorn_artifacts_dir` | `/home/ansiblecp/cluster-artifacts/longhorn` | Where rendered manifests are saved on CP |
| `cp_hostname` | `ansiblecp` | Control plane node name as shown by `kubectl get nodes` |
| `cp_ip` | `10.110.188.76` | Control plane IP |

---

## 7. How to Reset and Redeploy

Use this when you need to wipe Longhorn and start fresh.

```bash
# 1 — Uninstall the Helm release
helm uninstall longhorn -n longhorn-system

# 2 — Delete the namespace (removes all Longhorn CRDs and objects)
kubectl delete namespace longhorn-system

# 3 — Delete the custom StorageClass
kubectl delete storageclass longhorn-jupyterhomes

# 4 — Remove Longhorn data from worker nodes (run on each worker)
sudo rm -rf /var/lib/longhorn

# 5 — Remove artifact files on the control plane
rm -rf ~/cluster-artifacts/longhorn

# 6 — Redeploy
ansible-playbook -i inventory/hosts.yml site.yml
```

> **Warning:** Step 4 permanently deletes all volume data on the workers.
> Only do this if you are sure you do not need the data.

---

## 8. Verifying the Installation Manually

Run these commands on the control plane after deployment to confirm everything is healthy.

### All Longhorn pods running
```bash
kubectl get pods -n longhorn-system -o wide
```
Expected: every pod `Running` or `Completed`, no restarts.

### DaemonSets fully scheduled
```bash
kubectl get daemonset -n longhorn-system
```
Expected: `DESIRED` equals `READY` for `longhorn-manager` and `longhorn-csi-plugin`.

### StorageClass is default
```bash
kubectl get storageclass
```
Expected: only `longhorn-jupyterhomes` has `(default)`.

### Worker nodes recognized by Longhorn
```bash
kubectl get nodes.longhorn.io -n longhorn-system
```
Expected: workers show `ALLOWSCHEDULING=true` and `READY=true`.

### Helm release healthy
```bash
helm status longhorn -n longhorn-system
```
Expected: `STATUS: deployed`.

### Artifacts present
```bash
ls -lh ~/cluster-artifacts/longhorn/
```
Expected: `values.yaml`, `storageclass-jupyterhomes.yaml`, `test-pvc.yaml`.

---

## 9. Backup the Project Files

Run from your **local Windows machine** (PowerShell):

```powershell
# Backup the ansible-longhorn project
scp -rO ansiblecp@10.110.188.76:~/ansible-longhorn/ "C:\Users\SelmaB\Desktop\7.longhorn automation\ansible-longhorn-backup"

# Backup the rendered artifacts (values.yaml, StorageClass manifest)
scp -rO ansiblecp@10.110.188.76:~/cluster-artifacts/longhorn/ "C:\Users\SelmaB\Desktop\7.longhorn automation\longhorn-artifacts-backup"
```
