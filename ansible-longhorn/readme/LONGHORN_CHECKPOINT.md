# Longhorn Automation Checkpoint — Phase 2 Complete

**Date:** March 10, 2026
**Status:** ✅ Phase 2 Complete — Longhorn 1.7.2 fully automated and validated on test cluster

---

## What We Built

A fully self-contained Ansible project (`ansible-longhorn/`) that deploys Longhorn
distributed block storage on an existing Kubernetes cluster. Completely decoupled from
`ansible-k8s/` — no shared files, no shared roles, no dependencies between the two projects.

```
ansible-longhorn/
├── site.yml
├── inventory/hosts.yml
├── group_vars/all.yml
├── roles/
│   ├── longhorn_prereqs/tasks/main.yml
│   ├── helm/tasks/main.yml
│   └── longhorn/
│       ├── tasks/main.yml
│       └── templates/
│           ├── longhorn-values.yaml.j2
│           ├── storageclass-jupyterhomes.yaml.j2
│           └── test-pvc.yaml.j2
```

One command deploys the full storage stack:

```bash
ansible-playbook -i inventory/hosts.yml site.yml
```

---

## Test Cluster

| Node | Hostname | User | IP | Role |
|---|---|---|---|---|
| ansibleCp | ansibleCp | ansiblecp | 10.110.188.76 | Ansible control + K8s control plane |
| ansibleWorker | ansibleWorker | ansibleworker | 10.110.188.81 | K8s worker |

- Kubernetes: v1.30.5
- Longhorn: 1.7.2
- Helm: v3.20.0
- Replica count: 1 (1 schedulable worker)
- StorageClass: `longhorn-jupyterhomes` (default, Retain)

---

## Validated Run Output

```
PLAY RECAP
ansibleCp     : ok=30   changed=2   unreachable=0   failed=0   skipped=3
ansibleWorker : ok=6    changed=0   unreachable=0   failed=0   skipped=0
```

Every stage confirmed green:
- ✅ Node prerequisites satisfied on all nodes
- ✅ Helm v3.20.0 installed and verified
- ✅ Longhorn 1.7.2 deployed via Helm, all pods Running
- ✅ Worker node labeled for disk auto-registration
- ✅ `longhorn-jupyterhomes` is the single default StorageClass
- ✅ Validation PVC created, reached Bound, cleaned up

---

## Design Decisions

### 1. Total separation from ansible-k8s
`ansible-longhorn` is an independent project. It has its own inventory, its own
`group_vars/all.yml`, and its own roles. Nothing in `ansible-k8s` was touched.

Reason: cluster bootstrap and storage deployment are different operational layers.
Longhorn can be reinstalled, upgraded, or reconfigured without any risk to the cluster
playbook. The cluster can be rebuilt without touching storage automation.

### 2. Three-play structure in site.yml
- Play 1 targets `all` nodes — prerequisites must be in place on every node before
  any Helm work starts, because Longhorn DaemonSets start on workers immediately
  after the chart is installed.
- Play 2 targets `control_plane` only — Helm is a client-side tool, only needed on CP.
- Play 3 targets `control_plane` only — all kubectl and helm commands run from CP.

### 3. longhorn_prereqs role — what it does and why
| Task | Reason |
|---|---|
| Load `iscsi_tcp` module | Longhorn uses iSCSI to attach block volumes. Without it, volume attachment fails silently at mount time |
| Persist `iscsi_tcp` via `/etc/modules-load.d/` | Module must survive reboots or volumes fail to attach after restart |
| Install `cryptsetup` | Documented Longhorn prerequisite for device mapping. Required even if encryption is not used |
| Disable `multipathd` | Ubuntu 22.04 ships with it active. It claims iSCSI block devices before Longhorn can mount them, causing attachment failures. Safe to disable in lab/VM environments |
| Verify `iscsid` running | `open-iscsi` was installed by `ansible-k8s` node_prep but the daemon must be confirmed running |

### 4. Helm role — idempotent install
Checks `helm version --short` first. If Helm v3 is already present, download and
install steps are skipped entirely. Safe to rerun after any failure.

### 5. Longhorn role — full lifecycle
Every step is idempotent:
- Helm repo add: tolerates "already exists" without failing
- Namespace: uses `--dry-run=client -o yaml | kubectl apply` pattern
- Helm install: guarded by `helm status` check — skips if release exists
- Node labeling: uses `--overwrite` — safe to rerun
- StorageClass: uses `kubectl apply` — idempotent by design
- Default SC patch: no-op if annotation is already false
- Validation PVC: written to a template file, applied via `kubectl apply -f`,
  then fully cleaned up including the Released PV

### 6. StorageClass — diskSelector/nodeSelector omitted intentionally
The manual installation discovered that setting `diskSelector: ""` and
`nodeSelector: ""` causes Longhorn to reject PVC provisioning with:
`"specified disk tag does not exist"` → PVC stays Pending forever.
These parameters are absent from the template. This is the correct behavior
per Longhorn docs — omit them entirely when not using disk/node tagging.

### 7. Replica settings for test vs real cluster
| Setting | Test cluster (1 worker) | Real cluster (2 workers) |
|---|---|---|
| `longhorn_replica_count` | `1` | `2` |
| `longhorn_replica_soft_anti_affinity` | `"true"` | `"false"` |

With 1 worker and `replicaSoftAntiAffinity: false`, Longhorn strict mode has
nowhere to place a second replica and marks volumes Degraded. The test cluster
values are intentionally set to prevent this.

---

## Bugs Encountered and Fixed

| Bug | Root cause | Fix |
|---|---|---|
| Namespace task failed: `unknown shorthand flag: -f` | `command` module passes `\|` as a literal argument, not a shell pipe | Changed task to `shell` module |
| Node label failed: `nodes "ansibleWorker" not found` | Kubernetes registers nodes in lowercase; inventory hostname has mixed case | Added `\| lower` Jinja2 filter to node label loop |
| Validation PVC task failed: heredoc not interpreted | `command` module does not invoke a shell — `<<EOF` is passed raw to kubectl | Moved PVC manifest to a Jinja2 template, applied via `kubectl apply -f` |

---

## Artifacts on the Control Node

```
~/cluster-artifacts/longhorn/
├── values.yaml                     ← rendered Helm values used for install
├── storageclass-jupyterhomes.yaml  ← applied StorageClass manifest
└── test-pvc.yaml                   ← validation PVC manifest (reusable)
```

---

## Current State

- [x] Full Longhorn deployment automated end-to-end via single `ansible-playbook` command
- [x] All node prerequisites enforced before install
- [x] Helm v3 installed idempotently on control plane
- [x] Longhorn 1.7.2 deployed and all pods Running
- [x] Single default StorageClass (`longhorn-jupyterhomes`) confirmed
- [x] End-to-end PVC provisioning validated and cleaned up
- [x] Full rerun tested and confirmed idempotent (`failed=0`)

---

## Next Phase

Ingress Controller (nginx-ingress) → cert-manager → JupyterHub 3.x
