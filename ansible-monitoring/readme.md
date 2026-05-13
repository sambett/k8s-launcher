# Monitoring Stack

Observability for a kubeadm Kubernetes cluster, deployed and managed through
the k8s-launcher web interface. Covers cluster-level metrics, JupyterHub
session tracking, Longhorn storage, and GPU telemetry — all wired into a
single pre-built Grafana dashboard that is ready to use immediately after install.

---

## What it installs

### Layer 1 — kube-prometheus-stack (required)

Deployed as a single Helm chart into the `monitoring` namespace.

| Component | Role |
|---|---|
| **Prometheus** | Scrapes and stores metrics; answers PromQL queries |
| **Grafana** | Dashboard UI — reads from Prometheus via PromQL |
| **Alertmanager** | Deduplicates and routes alerts (email, Slack, etc.) |
| **Prometheus Operator** | Manages Prometheus/Alertmanager CRDs |
| **kube-state-metrics** | Exposes Kubernetes object state (pods, deployments, nodes) |
| **node-exporter** | DaemonSet per node; exposes CPU, memory, disk, and network metrics |

After install: Grafana on NodePort **32300**, Prometheus on NodePort **32301**.

Beyond the chart defaults, the following are also configured:

- **Longhorn scrape job** — Prometheus discovers and scrapes all Longhorn Manager
  pods individually (port 9500) so storage metrics appear for every node, not
  just whichever pod a load-balancer happened to route to.
- **kube-state-metrics label allowlist** — JupyterHub pod labels
  (`hub.jupyter.org/username`, `workbench/gitlab-group`, `workbench/profile-slug`)
  are added to the allowlist so per-user panels in Grafana can join on real
  usernames rather than raw pod names.

### Layer 2 — NVIDIA DCGM Exporter (optional, GPU nodes only)

Deployed as a DaemonSet on nodes matching a configurable GFD label
(`nvidia.com/gpu.mode=compute` by default). Runs in **Kubernetes-aware mode**
(`DCGM_EXPORTER_KUBERNETES=true`), which queries the Kubelet pod-resources
socket to discover which pod is using which GPU. Every metric is stamped with
`pod`, `namespace`, and `container` labels — giving real per-user GPU attribution
in Grafana, not just node-level totals.

Metrics exposed include GPU utilisation, framebuffer memory (used/free),
temperature, power draw, SM clock speed, and memory bandwidth utilisation.

When DCGM is deployed, the Helm chart is also upgraded to add a dedicated
Prometheus scrape job for DCGM pods.

---

## Grafana dashboards

Two dashboards are provisioned automatically — no manual import or UI clicks required.

### AI Workbench Monitor (always installed)

Sourced from `roles/prometheus_stack/files/workbench-dashboard.json`,
version-controlled in this repo. Ansible inlines it into a Helm ConfigMap at
deploy time; Grafana mounts and loads it on startup. **No internet access required.**

Available immediately after the base install under the **AI Workbench** folder.

| Section | What it shows |
|---|---|
| **GPU Monitoring** | GPU count, total power draw, avg temperature, avg VRAM used/free |
| **GPU Utilisation** | Core utilisation % and VRAM used over time, by GPU model and index |
| **CPU and RAM — by Node** | Current and historical CPU and RAM usage per cluster node |
| **JupyterHub Sessions** | Running, pending, failed, and restarting notebook counts |
| **Per-User Resource Usage** | Top RAM/CPU users now; RAM, CPU, GPU VRAM, and GPU utilisation per user over time |
| **Longhorn Storage** | Storage free %, per-disk usage, total capacity/used, read/write throughput |

The per-user GPU panels use DCGM Kubernetes mode labels to attribute VRAM and
utilisation to individual notebook pods. CPU-only notebooks show no data in GPU
panels — that is expected and correct.

### NVIDIA DCGM Exporter Dashboard (installed with DCGM)

The official NVIDIA dashboard (Grafana dashboard ID 12239), fetched from
grafana.com when Grafana starts. Requires internet access from the cluster.
Available under the **NVIDIA** folder after the DCGM install.

---

## Prerequisites

- `ansible-k8s` playbook complete — cluster running, nodes in `Ready` state
- `ansible-longhorn` playbook complete — persistent storage available
- `generated/inventory.ini` present (created during the launcher Configure step)

For GPU monitoring (DCGM), additionally:
- GPU node present with the NVIDIA Container Toolkit installed
- GPU Feature Discovery (GFD) running and node labels written
- NVIDIA Device Plugin running

The Monitoring tab displays a warning banner if the cluster is not ready.

---

## Using the Monitoring tab

Open the launcher at `http://<launcher-host>:5000` and select the **Monitoring** tab.

### Step 1 — Install Prometheus + Grafana

1. Confirm the status banner at the top is green (cluster reachable).
2. Under **Prometheus + Grafana**, check the chart version input.
   - Suggestion buttons appear automatically from `compat_matrix.json`.
   - The version recommended for your running Kubernetes minor version is
     marked with ★. Click it to fill the input, or type a version manually.
3. Click **Install / Upgrade** (≈ 5 min).
   - Both action buttons disable for the duration to prevent concurrent runs.
   - A live Ansible log streams below the button in real time.
   - The status dot turns green on success, red on failure.
4. On completion, an **Access** card appears with direct links:
   - Grafana: `http://<control-plane-ip>:32300` — default login `admin` / `prom-operator`
   - Prometheus: `http://<control-plane-ip>:32301`
5. Open Grafana → **AI Workbench** folder → **AI Workbench Monitor** — the
   dashboard is immediately populated with cluster, node, JupyterHub, and
   Longhorn data.

The button runs `helm upgrade --install`, which is idempotent — use it for
both first install and future chart version upgrades.

### Step 2 — Deploy DCGM Exporter (GPU nodes only)

1. Complete Step 1 first. DCGM requires Prometheus and Grafana to be running.
2. Under **NVIDIA DCGM Exporter**, select an image version from the suggestions.
   - DCGM version selection is driver-based, not Kubernetes-based.
   - The recommended entry is flagged directly in `compat_matrix.json`.
3. Click **Deploy DCGM Exporter** (≈ 3 min).
   - The launcher re-runs the full Helm upgrade with DCGM config added, then
     applies the DaemonSet manifest. The Helm step is idempotent — if the
     chart hasn't changed, it completes in under 30 seconds.
4. The DCGM badge updates to show live pod count (e.g. `✓ running 1/1`).
5. The **NVIDIA** folder in Grafana now contains the DCGM Exporter Dashboard.
6. The **Per-User** section of the AI Workbench Monitor gains live GPU VRAM
   and utilisation data attributed per notebook pod.

---

## Status badges

Badges refresh on tab load and after each install.

### Prometheus + Grafana

| Badge | Meaning |
|---|---|
| `✓ installed kube-prometheus-stack-X.Y.Z` | Helm release in `deployed` state |
| `not installed` | No Helm release found in the `monitoring` namespace |
| `error` | Launcher API unreachable |

### DCGM Exporter

| Badge | Meaning |
|---|---|
| `✓ running N/N` | DaemonSet running, all pods ready |
| `degraded 0/N` | DaemonSet exists but pod(s) not yet ready |
| `⚠ deployed · no GPU nodes found` | DaemonSet exists but `desiredNumberScheduled=0` — no node matches the nodeSelector. Check GFD labels or update `dcgm_node_selector_key/value` in `group_vars/all.yml`. |
| `not installed` | DaemonSet not present in the cluster |

---

## Configuration reference

### group_vars/all.yml

| Variable | Default | Description |
|---|---|---|
| `monitoring_namespace` | `monitoring` | Namespace for all monitoring components |
| `grafana_nodeport` | `32300` | NodePort for Grafana |
| `prometheus_nodeport` | `32301` | NodePort for Prometheus |
| `dcgm_namespace` | `monitoring` | Namespace for DCGM Exporter |
| `dcgm_metrics_port` | `9400` | Port DCGM Exporter serves metrics on |
| `dcgm_node_selector_key` | `nvidia.com/gpu.mode` | Node label key used to target GPU nodes |
| `dcgm_node_selector_value` | `compute` | Node label value to match |

### Runtime variables (injected by the launcher — do not set in group_vars)

| Variable | Example | Description |
|---|---|---|
| `chart_version` | `65.1.0` | kube-prometheus-stack Helm chart version |
| `dcgm_version` | `3.3.5-3.4.0-ubuntu22.04` | DCGM Exporter image tag |
| `deploy_dcgm` | `true` / `false` | Enables DCGM play and adds DCGM config to Helm values |

### Choosing the right nodeSelector

Run this on the control-plane node to list all labels GFD has written:

```bash
kubectl get node <gpu-node-name> --show-labels | tr ',' '\n' | grep nvidia
```

Pick a label with a fixed string value present on GPU nodes and absent on CPU-only nodes.

| GFD / NVDP version | Recommended label |
|---|---|
| NVDP ≥ 0.14 (current) | `nvidia.com/gpu.mode=compute` |
| NVDP ≥ 0.14 (alternative) | `nvidia.com/mig.capable=false` |
| NVDP < 0.14 (legacy) | `nvidia.com/gpu.present=true` |

---

## Running the playbook manually

The launcher uses `generated/inventory.ini` — no separate monitoring inventory exists.

```bash
# Base install — Prometheus + Grafana + AI Workbench dashboard
ansible-playbook ansible-monitoring/site.yml \
  -i generated/inventory.ini \
  --extra-vars "@generated/group_vars/all.yml" \
  --extra-vars "chart_version=65.1.0 deploy_dcgm=false"

# Full install — adds DCGM Exporter and NVIDIA dashboard
ansible-playbook ansible-monitoring/site.yml \
  -i generated/inventory.ini \
  --extra-vars "@generated/group_vars/all.yml" \
  --extra-vars "chart_version=65.1.0 dcgm_version=3.3.5-3.4.0-ubuntu22.04 deploy_dcgm=true"
```

Verify after deploy:

```bash
kubectl get pods -n monitoring -o wide
kubectl get pods -n monitoring -l app=dcgm-exporter -o wide
helm history kube-prometheus-stack -n monitoring
```

---

## Updating the AI Workbench dashboard

The JSON file is the source of truth — not the Grafana UI.

1. Make changes in the Grafana UI.
2. Export: **Dashboard settings → JSON Model → Copy to clipboard**.
3. Overwrite `roles/prometheus_stack/files/workbench-dashboard.json` with the new JSON.
4. Re-run the base install from the launcher. Helm re-renders the ConfigMap with
   the new JSON; Grafana picks it up on the next pod restart.

In-UI edits do not survive Helm upgrades — the ConfigMap is always regenerated
from the file in the repo.

---

## Troubleshooting

### "another operation (install/upgrade/rollback) is in progress"

A previous Helm run was interrupted, leaving the release in a pending state.

```bash
helm list -n monitoring -a
kubectl get secrets -n monitoring -l owner=helm,name=kube-prometheus-stack
# Patch the stuck secret to unblock Helm
kubectl patch secret <stuck-secret-name> -n monitoring \
  --type=merge -p '{"metadata":{"labels":{"status":"failed"}}}'
```

Then re-run the install from the launcher.

### DCGM badge shows "⚠ deployed · no GPU nodes found"

`desiredNumberScheduled=0` — no node matches the DaemonSet nodeSelector.

```bash
# Check what labels GFD wrote on the GPU node
kubectl get node <gpu-node> --show-labels | tr ',' '\n' | grep nvidia

# Check what nodeSelector the DaemonSet is currently using
kubectl get daemonset dcgm-exporter -n monitoring \
  -o jsonpath='{.spec.template.spec.nodeSelector}'
```

Update `dcgm_node_selector_key` and `dcgm_node_selector_value` in
`group_vars/all.yml`, then re-run the DCGM install.

### Per-user panels show no data

The per-user panels require the kube-state-metrics label allowlist. Verify it is active:

```bash
kubectl get deployment kube-prometheus-stack-kube-state-metrics \
  -n monitoring -o jsonpath='{.spec.template.spec.containers[0].args}' \
  | tr ',' '\n' | grep allowlist
```

The output must include `hub.jupyter.org/username`. If it is missing, the values
file was rendered without the `extraArgs` block — re-run the base install.

### Grafana NVIDIA dashboard shows "No data"

1. Confirm the DCGM pod is running:
   ```bash
   kubectl get pods -n monitoring -l app=dcgm-exporter
   ```
2. Open Prometheus at `http://<cp-ip>:32301/targets` — the `dcgm-exporter` job
   should show state **UP**.
3. If the job is missing entirely, the values file was rendered without the DCGM
   `additionalScrapeConfigs` block. Re-run the DCGM install (not just the base install).
4. If the job shows **DOWN**, the DCGM pod IP is unreachable from Prometheus.
   Check CNI network policy and pod CIDR routing.

### Helm upgrade fails with "nil pointer evaluating prometheusSpec"

`prometheusSpec` was rendered present but empty. Verify:

```bash
cat /tmp/monitoring-values.yaml | grep -A2 prometheusSpec
```

If `prometheusSpec:` appears with no children, the template has that block outside
the `{% if deploy_dcgm %}` conditional. Move it inside.
