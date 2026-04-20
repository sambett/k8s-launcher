# ansible-workers — Worker Node Lifecycle Management

> Part of the AI Workbench platform. Handles everything from a fresh Ubuntu VM
> to a fully operational Kubernetes worker node — and back to a clean VM.

---

## 1. Big Picture

### What problem this solves

Kubernetes does not provision nodes. It schedules workloads on nodes that
already exist, are already configured, and have already joined the cluster.
Getting a raw VM to that state requires coordinating six distinct layers:

- OS configuration (swap, kernel modules, sysctl)
- Container runtime (containerd, cgroup driver alignment)
- Kubernetes packages (kubelet, kubeadm at exact version)
- Storage prerequisites (iSCSI, multipathd, cryptsetup)
- Cluster join (token, stale state cleanup, registration)
- Post-join state (labels, inventory update, /etc/hosts propagation)

`ansible-workers` automates all six layers in the correct order, with
idempotency at every step, so the same playbook works whether you are adding
a node for the first time, re-adding one that failed halfway through, or
repairing a node that has drifted.

### Where ansible-workers fits in the platform

```
User (browser)
    │
    ▼
routes/worker.py          ← orchestrator: safety checks, token generation,
    │                        variable injection, SSE streaming to UI
    ▼
ansible-workers/          ← this project: pure node configuration
    │
    ├── add-worker.yml    → node_prep → longhorn_prereqs → containerd
    │                        → kubernetes_packages → worker_join
    │
    └── remove-worker.yml → worker_remove
         │
         ▼
    Worker VM             ← target: Ubuntu 22.04 or 24.04
         │
         ▼
    Kubernetes cluster    ← joins via control plane (ansiblecplane)
         │
         ├── Longhorn     ← discovers node via label, registers disk
         ├── Calico        ← schedules DaemonSet pod on new node
         └── JupyterHub   ← can now schedule notebook pods here
```

### High-level lifecycle

**Add worker:**
Fresh VM → SSH trust (Python) → OS prep → Runtime → K8s packages →
Storage prereqs → Join cluster → Labels → Inventory update → Validation

**Use worker:**
JupyterHub spawns a notebook pod → Kubernetes schedules it here →
Longhorn attaches a persistent volume → User works

**Remove worker:**
Safety check (Python) → Cordon → Drain → Delete → Longhorn cleanup →
VM reset → cluster_hosts cleanup → /etc/hosts cleanup → Inventory update

---

## 2. End-to-End Flow

### Adding a worker node

Think of it as a production line. Each stage produces a guarantee that the
next stage depends on. You cannot reorder the stages without breaking those
dependencies.

**Stage 0 — Python bootstrap (routes/worker.py, Step 1)**

Before Ansible touches the node, Python establishes SSH trust using the
operator's password — the only time a password is used. It:

- Reads and corrects the VM hostname if it doesn't match what was typed
- Adds `127.0.1.1 <hostname>` to `/etc/hosts` on the new VM
- Pushes the controller's public key to `authorized_keys`
- Writes a passwordless sudo rule
- Scans the node into `known_hosts`
- Wires control plane → new worker SSH for Ansible `delegate_to` tasks

After this stage the password is never needed again. All subsequent
communication is key-based.

**Stage 1 — node_prep**

The OS is configured so Kubernetes won't refuse to start.

- Swap is disabled. kubelet refuses to run on a node with swap active.
  Without this, kubelet starts, detects swap, and immediately crashes.
- Kernel modules `overlay` and `br_netfilter` are loaded and persisted.
  Without `br_netfilter`, iptables cannot see bridged pod traffic and
  Calico's network policies silently stop working.
- sysctl parameters are set to allow IP forwarding and bridge traffic.
- `/etc/hosts` is populated with all cluster nodes. Longhorn uses hostnames
  for iSCSI replica paths — if a node can't resolve a peer's hostname,
  volume replication silently fails.

**Stage 2 — longhorn_prereqs**

This must run **before** the node joins. The moment a node becomes Ready,
Longhorn's DaemonSet pod is scheduled on it and may immediately attempt
a volume attach. If iSCSI isn't ready at that moment, the attach fails.

- `open-iscsi` + `iscsid` running: userspace side of iSCSI
- `iscsi_tcp` kernel module loaded and persisted: kernel side of iSCSI.
  Without it, iSCSI sessions initiate without error but volume attaches
  hang indefinitely with no log message.
- `multipathd` disabled: Ubuntu ships with it active. It races with
  Longhorn to claim new block devices. If it wins, Longhorn cannot mount
  the volume. Symptom: volumes work on old nodes, silently fail on new ones.
- `cryptsetup` installed: a Longhorn OS prerequisite even when encryption
  is not in use. Omitting it causes subtle CSI attach failures.

**Stage 3 — containerd**

The container runtime is installed at the exact pinned version.

- The Docker GPG key and apt repository are added.
- `containerd.io` is installed at `{{ containerd_version }}` with
  `allow_downgrade: yes` — handles VMs that came pre-installed with a
  newer version from a failed previous attempt.
- `config.toml` is written with three required changes from the default:
  - `SystemdCgroup = true` — must match kubelet's cgroup driver. A mismatch
    causes pod OOM kills under memory pressure.
  - `config_path = "/etc/containerd/certs.d"` — enables per-registry config.
    Without this, the GitLab registry `hosts.toml` is never consulted and all
    notebook image pulls fail with an HTTP/HTTPS mismatch error.
  - `sandbox_image` pinned — prevents the pause container version from
    drifting between nodes, which causes spurious pod restarts.
- The config is verified by grep before containerd is restarted.
  If a future containerd release changes its default config format, the
  verification catches it immediately rather than silently producing a
  broken config.
- If GitLab is deployed, `hosts.toml` is written for the insecure registry.

**Stage 4 — kubernetes_packages**

kubelet and kubeadm are installed at the exact version matching the cluster.

- The Kubernetes apt repository for the correct minor version is added.
  Each minor version has its own repo URL — this prevents accidental minor
  version upgrades via `apt upgrade`.
- Only `kubelet` and `kubeadm` are installed. `kubectl` is deliberately
  omitted — workers don't need the CLI and installing it risks version
  confusion during troubleshooting.
- Both packages are held with `dpkg --set-selections`. A version skew
  between nodes breaks the cluster — kubeadm enforces strict N±1 minor
  version policy.
- kubelet is enabled but will fail to start here. This is expected and
  correct — systemd retries until kubeadm join writes `kubelet.conf`.

**Stage 5 — worker_join**

Three possible states are handled before running kubeadm join:

- **Case A**: `/etc/kubernetes/kubelet.conf` absent → clean node, join normally
- **Case B**: file present + kubelet active → already joined, skip join (idempotent re-run)
- **Case C**: file present + kubelet inactive → partial failed join.
  `kubeadm reset -f` cleans certificates, CNI state, and iptables rules.
  Without this, Ansible skips the join (thinking it's case B) and proceeds
  to labeling, which then fails with a confusing "node not found" error.

After join, node name is resolved by InternalIP (not `inventory_hostname`)
because kubelet registers under the VM's actual hostname, which may differ.
The lookup retries for 180 seconds — slow VMs can take over 60s to appear
in the API server.

Two labels are then applied:

- `node-role.kubernetes.io/worker=worker` — required for NodeSelector rules
- `node.longhorn.io/create-default-disk=true` — required for Longhorn to
  register this node's disk. Without it, the node is Ready in kubectl but
  invisible to Longhorn. No replicas will ever be placed here.

**Stage 6 — Python post-join (routes/worker.py, Steps 3–4)**

- Inventory updated: new worker inserted inside the `[workers]` section
- `cluster_hosts` updated in `generated/group_vars/all.yml`
- `/etc/hosts` propagated to all existing cluster nodes
- Six validation checks run before declaring success:
  node Ready, Longhorn discovery, Longhorn disk, calico-node pod Running,
  `iscsi_tcp` loaded, `multipathd` inactive

---

### Removing a worker node

The removal sequence is a contract. Each step is a precondition for the next.

**Step 1 — Cordon**
The node is marked unschedulable. The scheduler stops sending new pods here
while drain is running. Without this, pods evicted by drain are immediately
replaced on the same node — drain loops forever.

**Step 2 — Drain**
All existing pods are evicted gracefully. The Kubernetes scheduler
reschedules them on remaining nodes. DaemonSet pods are ignored (they are
cluster-managed and will be gone when the node is deleted). The 300s timeout
gives long-running workloads time to terminate cleanly rather than being
force-killed.

**Step 3 — Delete**
The node API object is removed. Kubernetes stops tracking it entirely.
This must happen after drain — deleting before draining orphans all pods
on the node with no rescheduling.

**Step 4 — Longhorn node entry removal**
Kubernetes forgot the node. Longhorn did not. It keeps `node.longhorn.io`
and keeps trying to schedule replicas there. With two workers, this halves
your effective replica count silently. `--ignore-not-found` makes this safe
even if Longhorn never fully registered the node.

**Step 5 — VM cleanup**
kubelet stopped, kubeadm reset, all state directories removed. This is
best-effort (`ignore_errors: yes` everywhere) because the VM may be
partially broken. You always want maximum cleanup rather than stopping
at the first error.

The key decision: containerd and Kubernetes packages are **not** removed.
This makes the VM immediately re-addable without a full reinstall.

**Step 6 — cluster_hosts cleanup**
The removed worker's entry is deleted from `cluster_hosts` in
`generated/group_vars/all.yml`. Without this, every future playbook run
(ansible-longhorn, ansible-k8s, ansible-workers) reads the stale entry and
re-writes the dead node back into `/etc/hosts` on all live nodes. If the IP
is later reused for a different VM, this causes silent hostname resolution
errors across the cluster.

**Step 7 — /etc/hosts cleanup on remaining nodes**
The removed worker's `ip hostname` line is deleted from `/etc/hosts` on
every remaining cluster node via Ansible ad-hoc lineinfile. This runs before
the inventory is updated so all remaining nodes are still reachable.
Longhorn inter-replica iSCSI paths and Ansible delegate_to tasks both rely
on hostname resolution — stale entries cause timeouts that are difficult to
diagnose.

---

## 3. Design Philosophy

### Idempotency

Every task is safe to re-run. File writes use `copy` or `lineinfile`, not
`shell echo`. Packages use `state: present`. kubeadm join has three-state
detection. Labels use `--overwrite`. Re-running the playbook on a healthy
node produces no changes and no errors.

### Version pinning + hold

`kubernetes_version` and `containerd_version` flow from the user's input
in the Configure tab → `generated/group_vars/all.yml` → injected via
`--extra-vars` at runtime. The static fallbacks in
`ansible-workers/group_vars/all.yml` are never used in production — they
document the validated baseline and enable manual runability.

`dpkg --set-selections hold` prevents `apt upgrade` from silently advancing
a node to a newer minor version. A single out-of-version node can cause
kubeadm to refuse operations and kubectl to produce confusing API errors.

### Separation of concerns

Each role owns exactly one layer. `node_prep` knows nothing about containerd.
`containerd` knows nothing about Kubernetes. `kubernetes_packages` knows
nothing about Longhorn. This means each role can be re-run independently
for repair without triggering unrelated side effects.

### Strict vs best-effort

Cluster-side operations (cordon, drain, delete) have no `ignore_errors`.
If drain fails, something is wrong and you need to know. The cluster must
be in a clean state before the node object is removed.

VM-side cleanup operations all have `ignore_errors: yes`. The VM may be
partially broken. You want maximum cleanup regardless of individual failures.

### OS homogeneity

All cluster nodes must run the same Ubuntu LTS version. This is not a
preference — Longhorn's kernel modules (iSCSI initiator, device-mapper)
are compiled against specific kernel versions. Ubuntu 22.04 ships kernel
5.15, Ubuntu 24.04 ships kernel 6.8. A mixed-OS cluster produces
non-deterministic storage failures that are extremely difficult to diagnose
because they only manifest under specific storage operations.

AWS EKS, GKE, and NVIDIA AI Workbench all enforce the same constraint.

### No kubectl on workers

Workers are pure compute. The API server lives on the control plane.
Installing kubectl on workers creates a misleading interface — it would
work only if a kubeconfig was manually placed there, which introduces a
security concern (cluster credentials on every worker). All cluster
operations are delegated to the control plane via `delegate_to`.

---

## 4. Role-by-Role Mental Models

### node_prep — "Make the OS acceptable to Kubernetes"

kubelet has hard requirements. If swap is active, it refuses to start.
If `br_netfilter` isn't loaded, network policies silently break. If
`/etc/hosts` is missing cluster peer entries, Longhorn replica paths fail.
This role satisfies all of kubelet's preconditions before anything
Kubernetes-related is installed.

**Skip it:** kubelet crashes on start, Longhorn replicas fail intermittently,
and cluster DNS may not resolve correctly between nodes.

### containerd — "Install and align the runtime with the rest of the cluster"

containerd is not just a runtime — its configuration must be precisely
aligned with kubelet (cgroup driver), with the cluster's pause image version,
and with the GitLab registry's HTTP-only setup. A default containerd install
fails on all three counts.

**Skip it:** kubelet has no runtime to talk to and never starts.
Wrong cgroup driver: pods OOM-kill under memory pressure with no clear cause.
Missing registry config: all notebook image pulls fail immediately.

### kubernetes_packages — "Install the exact versions the cluster expects"

kubelet is the node agent. kubeadm is the join tool. Both must be at the
exact same minor version as the control plane. The apt repository is
versioned by minor version specifically to prevent accidental upgrades.

**Skip it:** kubeadm join command doesn't exist, kubelet can't join.
Wrong version: kubeadm enforces version skew policy and refuses to join.

### longhorn_prereqs — "Prepare the node for storage attachment"

Longhorn uses iSCSI to attach block volumes to nodes. iSCSI requires both
a userspace daemon (`iscsid`) and a kernel module (`iscsi_tcp`). `multipathd`
must be disabled to prevent it from claiming Longhorn's block devices.
This must all be in place before the node joins, because Longhorn's DaemonSet
schedules immediately on Ready nodes.

**Skip it:** volume attaches hang silently. The node looks healthy. Longhorn
shows replicas as scheduled. Nothing works and there are no useful logs.

### worker_join — "Safely join, handling all possible prior states"

This role is the only one that touches the cluster. Its key insight is
that a failed previous join leaves the system in a worse state than a
fresh node — it must detect and clean that state before proceeding.
Node name resolution by IP rather than hostname makes labeling robust
against hostname drift.

**Skip stale state cleanup:** re-add fails with a confusing error unrelated
to the actual problem. Skip the retry window: labeling races the API
server and fails on slow VMs.

### worker_remove — "Remove without leaving ghosts, clean without data loss"

The removal sequence is not about being polite — each step is a hard
precondition for the next. The Longhorn node entry cleanup is the most
commonly missed step and the one with the most subtle failure mode.
The `/var/lib/longhorn` cleanup is what makes re-add reliable.

**Skip Longhorn entry removal:** ghost node silently reduces effective
replica count. Skip `/var/lib/longhorn` cleanup: re-add produces
phantom disk usage and replica scheduling failures.

---

## 5. Failure Scenarios This Design Prevents

### Partial join state (Case C)

**What happens:** A VM runs kubeadm join, which writes `kubelet.conf`, but
kubelet crashes before registration completes. On re-run, a naive idempotency
check sees the file and skips the join. Ansible proceeds to labeling, finds
no node with that IP, and fails with "node not found" — completely obscuring
the real problem.

**How we prevent it:** Three-state detection in `worker_join`. If `kubelet.conf`
exists but kubelet is inactive, `kubeadm reset -f` cleans everything and the
join runs from scratch.

### Version drift between nodes

**What happens:** A worker is added months after the cluster was provisioned.
Its containerd was upgraded by an unattended-upgrade run. Its kubelet is now
at a different patch version. Subtle API incompatibilities emerge. kubeadm
may refuse upgrade operations citing version skew.

**How we prevent it:** Version pinning via group_vars + dpkg hold on all
nodes. The `allow_downgrade: yes` flag on containerd install handles the
case where a VM already has a newer version installed.

### Longhorn silent iSCSI failure

**What happens:** `iscsi_tcp` isn't loaded. `iscsid` starts, iSCSI sessions
initiate, Longhorn schedules replicas on the new node — all without errors.
Volume attaches hang indefinitely. The Longhorn UI shows the replica as
scheduled. No useful log message exists.

**How we prevent it:** `longhorn_prereqs` explicitly loads `iscsi_tcp` and
persists it. Post-join validation in `routes/worker.py` checks it's loaded
and reports clearly if not.

### multipathd device theft

**What happens:** `multipathd` is active (Ubuntu default). Longhorn attaches
a volume via iSCSI. The kernel presents `/dev/sdb`. `multipathd` races to
claim it for multipath management before Longhorn's CSI driver can use it.
Once claimed, Longhorn cannot mount the device. Symptom: works on old nodes,
silently fails on new ones.

**How we prevent it:** `multipathd` is explicitly stopped and disabled in
`longhorn_prereqs`, before the node joins.

### Image pull failures on new workers

**What happens:** A worker is added before JupyterHub is deployed. The
containerd registry config is not written. JupyterHub is deployed later.
Users spawn notebooks. The notebook pod is scheduled on the new worker.
Image pull fails with "http: server gave HTTP response to HTTPS client".
The failure only manifests on the specific node that was added early.

**How we prevent it:** `_get_registry_host()` in `routes/worker.py` checks
both `jupyterhub-vars.yml` and `gitlab-outputs.json` as fallback, so registry
config is written whenever GitLab exists — not just when JupyterHub exists.

### Ghost nodes after removal

**What happens:** `kubectl delete node` removes the Kubernetes object. Longhorn
does not receive any notification. It keeps the `node.longhorn.io` object and
keeps attempting to schedule replicas there. With two workers, this silently
halves the effective replication factor. Volumes appear healthy. They are not.

**How we prevent it:** Explicit `kubectl delete node.longhorn.io` as a
dedicated step in `worker_remove`, with `ignore_errors` to handle the case
where Longhorn never registered the node.

### Stale Longhorn data on re-add

**What happens:** A worker is removed. It is re-added weeks later. Longhorn
finds `/var/lib/longhorn` on the disk with data from old replicas. It
reports incorrect disk usage and may refuse to schedule new replicas because
it calculates the disk as over-committed.

**How we prevent it:** `worker_remove` explicitly deletes `/var/lib/longhorn`.
The re-add flow starts with a disk that Longhorn has never seen before.

---

## 6. Role of routes/worker.py

`routes/worker.py` is the orchestrator. Ansible is the executor.
This distinction matters: Ansible cannot make decisions about cluster state,
Longhorn replica counts, or SSH trust setup. Python can.

### What routes/worker.py does that Ansible cannot

**Dynamic join token generation:**
kubeadm tokens expire after 24 hours. If the token was generated at cluster
init time and stored statically, any add-worker run more than 24 hours later
silently fails. `routes/worker.py` generates a fresh token via `run_on_cp`
immediately before calling the playbook — token expiry is never a concern.

**Longhorn replica safety check:**
Before any removal runs, Python queries the Longhorn API and calculates
whether removing the target node would leave any volume with zero healthy
replicas. If yes, the entire operation is aborted with a clear explanation.
Ansible has no way to make this calculation — it would need to parse
Kubernetes API responses and apply business logic.

**SSH trust bootstrap:**
The password is used exactly once, via Paramiko. Ansible requires key-based
auth to be in place before it can run. Python bridges the gap — it uses the
password to install the key, then hands off to Ansible which never sees
the password.

**Variable injection:**
The `join_command` and `gitlab_registry_host` are runtime values that cannot
exist in static group_vars. They are injected via `--extra-vars` at the
moment of playbook invocation.

**SSE streaming:**
Ansible's stdout is piped through `subprocess.Popen` and forwarded
line-by-line to the browser as Server-Sent Events. The UI shows live
progress without polling.

### Add-worker orchestration

```
routes/worker.py  _add_worker_stream()
    │
    ├── Step 1: Paramiko SSH bootstrap (password-based, one time)
    │           hostname correction, sudo setup, key push, known_hosts
    │
    ├── Step 2: ansible-playbook add-worker.yml
    │           --extra-vars @generated/group_vars/all.yml
    │           --extra-vars join_command=<fresh token>
    │           --extra-vars gitlab_registry_host=<if GitLab deployed>
    │
    ├── Step 3: Inventory + group_vars + /etc/hosts update
    │
    └── Step 4: Six-point validation before declaring success
```

### Remove-worker orchestration

```
routes/worker.py  _remove_worker_stream()
    │
    ├── Longhorn replica safety check (abort if data loss risk)
    │
    ├── Worker count warning (remaining workers < longhorn_replica_count)
    │
    ├── ansible-playbook remove-worker.yml
    │           cordon → drain → delete → Longhorn cleanup → VM cleanup
    │
    ├── Remove cluster_hosts entry from generated/group_vars/all.yml
    ├── Remove /etc/hosts entry from all remaining cluster nodes
    └── Inventory update (remove worker entry)
```

---

## 7. Re-add / Repair Logic

### Why containerd and k8s packages are not removed

Removal leaves these in place deliberately. containerd and kubelet are
version-pinned and held. They are valid for this cluster. Removing and
reinstalling them during re-add adds 3–5 minutes of package download time
with no benefit. The containerd role's `allow_downgrade: yes` handles any
edge case where the version has drifted.

### What "clean state" means in this system

A node is in clean state when all of the following are true:

| Path / Resource | Expected state |
|---|---|
| `/etc/kubernetes` | Does not exist |
| `/var/lib/kubelet` | Does not exist |
| `/etc/cni/net.d` | Does not exist |
| `/var/lib/longhorn` | Does not exist |
| iptables rules (kube-proxy) | Flushed by kubeadm reset |
| `kubectl get node <name>` | Not found |
| `node.longhorn.io/<name>` | Not found |

A node that satisfies all seven conditions can be re-added via the add-worker
flow and will behave identically to a brand-new VM.

### Why /var/lib/longhorn must be cleaned

Longhorn does not distinguish between "replica data from when this node was
part of the cluster" and "new empty disk". If stale replica data exists when
the node rejoins, Longhorn reads the metadata, calculates disk usage based on
old replica sizes, and may refuse to schedule new replicas because the disk
appears over-committed. The only safe state is an empty path that Longhorn
initializes fresh on first contact.

---

## 8. Key Takeaways

- **The launcher is the brain, Ansible is the hands.** Python makes
  decisions (safety checks, token generation, variable injection). Ansible
  executes them. Neither can do the other's job.

- **Role order is a hard dependency chain.** OS prep → runtime → packages →
  storage prereqs → join. Each stage produces guarantees the next requires.

- **Idempotency is not optional.** Every task must be re-runnable. Failed
  provisioning runs are normal. The system must handle re-runs cleanly
  without manual intervention.

- **Version consistency is a cluster invariant.** One out-of-version node
  can break the entire cluster. Pinning + hold + single source of truth
  (`generated/group_vars/all.yml`) enforce this.

- **Longhorn has more state than Kubernetes.** Removing a node from kubectl
  does not remove it from Longhorn. Both must be cleaned explicitly.

- **Storage prerequisites must precede join.** Longhorn's DaemonSet
  schedules immediately on Ready nodes. If iSCSI isn't ready, the first
  volume attach fails silently.

- **OS homogeneity is non-negotiable.** Kernel module behavior differs
  between Ubuntu 22.04 (5.15) and 24.04 (6.8). Mixed OS produces
  non-deterministic Longhorn failures.

- **Clean removal enables clean re-add.** The remove flow is designed with
  re-add in mind. Packages are preserved. State is fully wiped. Re-add
  takes minutes, not hours.

- **Strict on cluster operations, lenient on VM cleanup.** Drain must
  succeed. Deleting stale directories should not stop if one is missing.
  The asymmetry is intentional and correct.

- **The 180-second retry window is not pessimism.** kubelet registration
  is async. Under real cluster load on KVM, registration takes 60–120s.
  Failing fast here produces misleading errors and leaves nodes joined
  but unlabeled — functionally broken but invisible.
