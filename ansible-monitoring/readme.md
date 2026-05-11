# Monitoring Stack — k8s-launcher

> **Status:** Fully deployed and verified.
> kube-prometheus-stack 65.1.0 running · DCGM Exporter running on w1-temp (NVIDIA A2)
> Grafana → NodePort 32300 · Prometheus → NodePort 32301

This document covers everything you need to understand, use, and maintain the
monitoring stack: what it installs, how the launcher UI works, what each API
endpoint does, how GPU metrics flow into Grafana, and how to troubleshoot it.

---

## Table of contents

1. [What this installs](#1-what-this-installs)
2. [Prerequisites](#2-prerequisites)
3. [Using the Monitoring tab in the launcher](#3-using-the-monitoring-tab-in-the-launcher)
4. [Status badges and what they mean](#4-status-badges-and-what-they-mean)
5. [Version selection and compat_matrix.json](#5-version-selection-and-compat_matrixjson)
6. [How GPU metrics reach Grafana](#6-how-gpu-metrics-reach-grafana)
7. [Grafana dashboard provisioning](#7-grafana-dashboard-provisioning)
8. [API reference](#8-api-reference)
9. [Running the playbook manually](#9-running-the-playbook-manually)
10. [Architecture — Ansible roles and files](#10-architecture--ansible-roles-and-files)
11. [Configuration reference](#11-configuration-reference)
12. [Concurrency protection](#12-concurrency-protection)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. What this installs

The monitoring stack is split into two independent layers.

### Layer 1 — kube-prometheus-stack (always installed)

A single Helm chart that installs the complete Kubernetes monitoring platform.
Ansible does not build these components — it calls `helm upgrade --install`
and the chart manages everything.

| Component | Role |
|---|---|
| **Prometheus** | Scrapes metrics from all targets every 15–60s, stores them as time-series, answers PromQL queries |
| **Alertmanager** | Receives firing alerts from Prometheus, deduplicates and routes them (email, Slack, etc.) |
| **Grafana** | Web UI for dashboards and visualisations. Reads from Prometheus via PromQL |
| **Prometheus Operator** | Kubernetes controller. Manages Prometheus and Alertmanager CRDs (PrometheusRule, ServiceMonitor, etc.) |
| **kube-state-metrics** | Exposes Kubernetes object state as metrics: pod counts, deployment replicas, node conditions, resource requests |
| **node-exporter** | DaemonSet on every node. Exposes OS-level metrics: CPU, RAM, disk I/O, network throughput |

After install, Grafana is available on NodePort **32300** and Prometheus on **32301**.

### Layer 2 — DCGM Exporter (optional, GPU nodes only)

The base chart gives cluster and node metrics but has no knowledge of GPU hardware.
NVIDIA GPU telemetry requires a separate component: **DCGM Exporter**.

It runs as a DaemonSet on GPU nodes only (identified by a GFD label) and exposes:

| Metric | Description |
|---|---|
| `DCGM_FI_DEV_GPU_UTIL` | GPU core utilisation % |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | GPU memory bandwidth utilisation % |
| `DCGM_FI_DEV_FB_USED` | Framebuffer memory used (MiB) |
| `DCGM_FI_DEV_FB_FREE` | Framebuffer memory free (MiB) |
| `DCGM_FI_DEV_GPU_TEMP` | GPU temperature (°C) |
| `DCGM_FI_DEV_POWER_USAGE` | Power draw (W) |
| `DCGM_FI_DEV_SM_CLOCK` | Streaming multiprocessor clock (MHz) |

Prometheus scrapes DCGM Exporter pods on port **9400**. Grafana receives the
NVIDIA dashboard (gnetId 12239) automatically via dashboard provisioning.

---

## 2. Prerequisites

The Monitoring tab requires a running Kubernetes cluster. If the cluster is not
deployed, the tab shows an amber warning banner with a link to the Kubernetes tab.

Specifically required before using this tab:
- `ansible-k8s` playbook completed (cluster running, nodes Ready)
- `ansible-longhorn` playbook completed (storage available)
- `generated/inventory.ini` exists (created by the launcher Configure step)

For GPU monitoring (DCGM), additionally required:
- NVIDIA GPU node present in the cluster with the NVIDIA Container Toolkit installed
- GPU Feature Discovery (GFD) running and node labels written
- NVIDIA Device Plugin running

---

## 3. Using the Monitoring tab in the launcher

The Monitoring tab is at `http://<launcher-ip>:5000`. It has two sections.

### Section 1 — Prometheus + Grafana

This installs or upgrades the kube-prometheus-stack Helm chart.

**Step-by-step:**

1. Open the **Monitoring** tab in the launcher.
2. The tab auto-checks the cluster status. The banner at the top turns green
   when the cluster is ready.
3. Under **Prometheus + Grafana**, look at the chart version input field.
   - Version suggestion buttons appear automatically below the input, pulled
     from `compat_matrix.json`. The recommended version for your Kubernetes
     minor version is highlighted with a star (★).
   - Click a suggestion to fill the field, or type the version manually.
4. Click **Install / Upgrade** (~5 min).
   - The button and the DCGM button both disable immediately to prevent
     concurrent installs.
   - A live log stream appears below showing Ansible task output in real time.
   - A coloured dot next to the button shows progress:
     - Grey = idle
     - Spinning = running
     - Green = success
     - Red = failed
5. When complete, an **Access** card appears with direct links to:
   - Grafana: `http://<cp-ip>:32300` — login `admin` / `prom-operator`
   - Prometheus: `http://<cp-ip>:32301`

**When to use Install vs Upgrade:**
The button is labelled "Install / Upgrade" because it runs `helm upgrade --install`
which is idempotent — it installs if the release does not exist, upgrades if it does.
Use it for first install and for any future chart version changes.

### Section 2 — NVIDIA DCGM Exporter

This deploys the DCGM Exporter DaemonSet on GPU nodes.

**Step-by-step:**

1. Complete Section 1 first. DCGM requires kube-prometheus-stack to already
   be running (it needs Prometheus and Grafana).
2. Under **NVIDIA DCGM Exporter**, look at the image version input field.
   - Suggestion buttons appear from `compat_matrix.json`. The recommended
     version is pre-marked. Click to fill.
3. Click **Deploy DCGM Exporter** (~3 min).
   - Both action buttons disable during the stream.
   - The Ansible log shows the Helm upgrade (idempotent, fast if nothing changed)
     followed by the DaemonSet apply.
4. When complete, the DCGM badge updates to show running pod count (e.g. `✓ running 1/1`).
5. Open Grafana → **NVIDIA** folder → **DCGM Exporter Dashboard** to see live GPU metrics.

**Note:** The DCGM install also re-runs the Helm chart upgrade. This is intentional —
it adds the DCGM scrape config and NVIDIA dashboard provisioning to the values file,
which requires a Helm upgrade to take effect. Helm is idempotent so if nothing else
changed this step completes in under 30 seconds.

---

## 4. Status badges and what they mean

Each section has a coloured badge in its header that refreshes automatically
when the tab loads and after each install completes.

### Prometheus + Grafana badge

| Badge text | Meaning |
|---|---|
| `checking...` | API call in flight |
| `✓ installed kube-prometheus-stack-65.1.0` | Helm release in `deployed` state |
| `not installed` | No Helm release found |
| `error` | Could not reach the launcher API |

### DCGM Exporter badge

| Badge text | Meaning |
|---|---|
| `checking...` | API call in flight |
| `✓ running 1/1` | DaemonSet running, all pods ready |
| `degraded 0/1` | DaemonSet exists but pod not yet ready |
| `⚠ deployed · no GPU nodes found` | DaemonSet exists but desiredNumberScheduled=0 — no node currently matches the nodeSelector. GFD may not have run yet, or the nodeSelector key needs updating in `group_vars/all.yml` |
| `not installed` | DaemonSet does not exist in the cluster |
| `error` | Could not reach the launcher API |

---

## 5. Version selection and compat_matrix.json

Version suggestions are driven entirely by `compat_matrix.json` in the repo root.
The launcher reads this file and filters/marks versions based on the running
cluster's Kubernetes minor version.

### kube-prometheus-stack versions

```json
"kube_prometheus_stack": [
  {
    "version": "65.1.0",
    "k8s_min": "1.29",
    "k8s_max": "1.31",
    "notes": "Recommended for K8s 1.30.x"
  },
  ...
]
```

The launcher reads the current cluster's k8s minor version and marks whichever
entry's `k8s_min`–`k8s_max` range covers it as the recommended version (★).

### DCGM Exporter versions

```json
"dcgm_exporter": [
  {
    "version": "3.3.5-3.4.0-ubuntu22.04",
    "recommended": true,
    "notes": "Compatible with NVIDIA driver 590+ and CUDA 13.x"
  },
  ...
]
```

DCGM version selection is driver-based, not Kubernetes-based. The `recommended: true`
flag is set directly in the matrix entry. To add a new DCGM version, append an entry
to the `dcgm_exporter` array and set `recommended: true` on the new entry.

**Version string format for DCGM:** `<dcgm-version>-<driver-version>-<os>`
Example: `3.3.5-3.4.0-ubuntu22.04` — DCGM 3.3.5, driver 3.4.0, Ubuntu 22.04 base image.

---

## 6. How GPU metrics reach Grafana

```
NVIDIA GPU hardware
       |
       v
NVIDIA kernel driver  ──►  DCGM daemon (reads hardware counters)
                                  |
                                  v
                    DCGM Exporter pod (DaemonSet on GPU nodes)
                    - Runs on w1-temp (nvidia.com/gpu.mode=compute)
                    - Serves /metrics on port 9400
                                  |
                                  v  (scraped every 15s)
                           Prometheus
                    - Job: dcgm-exporter (additionalScrapeConfigs)
                    - Discovers pods via kubernetes_sd_configs
                    - Stores metrics as time-series
                                  |
                                  v  (PromQL queries)
                             Grafana
                    - NVIDIA folder → DCGM Exporter Dashboard
                    - gnetId 12239, auto-provisioned at startup
                                  |
                                  v
                    Researcher browser (NodePort 32300)
```

### Why DCGM is a separate DaemonSet

The kube-prometheus-stack chart monitors the Kubernetes cluster and its nodes.
It has no awareness of GPU hardware. GPU metrics come from NVIDIA's DCGM
(Data Center GPU Manager) daemon which runs inside the GPU node's kernel driver.
DCGM Exporter bridges that daemon to Prometheus by reading the counters and
serving them in Prometheus text format on port 9400.

### How Prometheus discovers DCGM pods

When `deploy_dcgm=true`, the Helm values file gains an `additionalScrapeConfigs`
block that tells Prometheus to use Kubernetes service discovery to find pods
labelled `app=dcgm-exporter` in the monitoring namespace, then scrape each one
at `<pod-ip>:9400/metrics`. This happens entirely within the cluster — no
external configuration needed.

---

## 7. Grafana dashboard provisioning

Grafana supports automatic dashboard provisioning — dashboards are declared in
configuration and imported at startup. No manual clicking in the Grafana UI is
required.

When `deploy_dcgm=true`, the Helm values include:

```yaml
grafana:
  dashboardProviders:
    dashboardproviders.yaml:
      providers:
        - name: nvidia           # Creates "NVIDIA" folder in Grafana
          type: file
          options:
            path: /var/lib/grafana/dashboards/nvidia

  dashboards:
    nvidia:
      dcgm-exporter:
        gnetId: 12239            # Downloads from grafana.com/grafana/dashboards/12239
        revision: 1
        datasource: Prometheus
```

**What is gnetId?**
Every dashboard published to grafana.com/grafana/dashboards has a numeric ID.
When Grafana starts, it fetches the JSON definition of that dashboard from the
Grafana.com API and imports it automatically. gnetId 12239 is the official
NVIDIA DCGM Exporter dashboard, which shows GPU utilisation, memory, temperature,
power, and clock speed broken down per GPU and per node.

**When `deploy_dcgm=false`**, both the `dashboardProviders` and `dashboards`
blocks are completely absent from the rendered values file. Grafana starts
cleanly with no NVIDIA folder and no dangling dashboard.

### Adding a custom Grafana dashboard

Option A — reference a public Grafana.com dashboard by gnetId:
Add an entry under `dashboards.nvidia` in `values.yaml.j2`:
```yaml
my-custom-dash:
  gnetId: <id>
  revision: 1
  datasource: Prometheus
```

Option B — version-control your own dashboard JSON (recommended):
1. Build the dashboard in the Grafana UI
2. Export it: Dashboard > Share > Export > Save to file
3. Save the JSON to `roles/prometheus_stack/files/my-dashboard.json`
4. Add a Jinja2 file lookup entry in the `dashboards` block in `values.yaml.j2`

Option B gives version history, peer review, and fully reproducible deploys.

---

## 8. API reference

All endpoints are served by `routes/monitoring.py` at `http://<launcher>:5000`.

### GET /api/monitoring/status

Returns whether kube-prometheus-stack is installed via Helm.

Response:
```json
{ "status": "installed", "chart": "kube-prometheus-stack-65.1.0" }
{ "status": "not_installed", "chart": "" }
```

### GET /api/monitoring/versions

Returns chart versions from `compat_matrix.json`, filtered and marked by
the running cluster's Kubernetes minor version.

Response:
```json
{
  "versions": [{"version": "65.1.0", "k8s_min": "1.29", "k8s_max": "1.31", "notes": "..."}],
  "recommended": "65.1.0",
  "k8s_version": "1.30"
}
```

### GET /api/monitoring/access

Returns live Grafana and Prometheus NodePort URLs by querying the running services.

Response:
```json
{
  "grafana_url": "http://10.110.188.85:32300",
  "prometheus_url": "http://10.110.188.85:32301"
}
```

### GET /api/monitoring/install/stream?version=65.1.0

SSE stream. Runs `ansible-monitoring/site.yml` with `deploy_dcgm=false`.
Installs or upgrades kube-prometheus-stack.

Stream tokens:
- `data: <ansible output line>` — live log line
- `data: __DONE__` — playbook completed successfully
- `data: __ERROR__:LOCKED` — another monitoring operation is already running
- `data: __ERROR__:<N>` — playbook exited with return code N

### GET /api/monitoring/dcgm/status

Returns the DCGM Exporter DaemonSet state.

Response:
```json
{ "status": "ready",        "ready": 1, "desired": 1 }
{ "status": "degraded",     "ready": 0, "desired": 1 }
{ "status": "no_gpu_nodes", "ready": 0, "desired": 0 }
{ "status": "not_installed","ready": 0, "desired": 0 }
```

`no_gpu_nodes` means the DaemonSet exists and is healthy but no node currently
matches the nodeSelector. This is not the same as not installed.

### GET /api/monitoring/dcgm/versions

Returns DCGM Exporter versions from `compat_matrix.json`.

Response:
```json
{
  "versions": [{"version": "3.3.5-3.4.0-ubuntu22.04", "recommended": true, "notes": "..."}],
  "recommended": "3.3.5-3.4.0-ubuntu22.04"
}
```

### GET /api/monitoring/dcgm/install/stream?version=3.3.5-3.4.0-ubuntu22.04

SSE stream. Runs `ansible-monitoring/site.yml` with `deploy_dcgm=true`.
Upgrades kube-prometheus-stack (adds DCGM config) and deploys the DaemonSet.

Same stream tokens as the base install stream.

---

## 9. Running the playbook manually

All playbooks use `generated/inventory.ini` — the inventory generated by the
launcher during cluster configuration. There is no separate monitoring inventory.

### Base install (Prometheus + Grafana only)

```bash
cd ~/k8s-launcher
ansible-playbook ansible-monitoring/site.yml \
  -i generated/inventory.ini \
  --extra-vars "@generated/group_vars/all.yml" \
  --extra-vars "chart_version=65.1.0 deploy_dcgm=false"
```

### DCGM install (adds GPU monitoring)

```bash
cd ~/k8s-launcher
ansible-playbook ansible-monitoring/site.yml \
  -i generated/inventory.ini \
  --extra-vars "@generated/group_vars/all.yml" \
  --extra-vars "chart_version=65.1.0 dcgm_version=3.3.5-3.4.0-ubuntu22.04 deploy_dcgm=true"
```

### Verify after deploy

```bash
# All monitoring pods
kubectl get pods -n monitoring -o wide

# DCGM pod specifically
kubectl get pods -n monitoring -l app=dcgm-exporter -o wide

# Helm release history
helm history kube-prometheus-stack -n monitoring

# Rendered values file (check before running if debugging)
cat /tmp/monitoring-values.yaml
```

---

## 10. Architecture — Ansible roles and files

```
ansible-monitoring/
  site.yml                          Entry point. Three plays in order.
  group_vars/all.yml                Static config (NodePorts, namespaces, nodeSelector)
  inventory/                        NOT used by launcher — for reference only
  roles/
    prometheus_stack/
      tasks/main.yml                Creates namespace, adds Helm repo, pre-flight
                                    status check, renders values, helm upgrade
      templates/values.yaml.j2      Helm values: NodePorts + conditional DCGM blocks
      defaults/main.yml             Role defaults
    dcgm_exporter/
      tasks/main.yml                Renders manifest, kubectl apply, waits for rollout
      templates/dcgm.yaml.j2        DaemonSet + headless Service manifest
    validate/
      tasks/main.yml                Checks pod readiness, prints pod/service tables
```

### Key design decisions

**values.yaml.j2 conditional blocks**
Both the DCGM scrape config and the Grafana NVIDIA dashboard block are wrapped
in `{% if deploy_dcgm | default(false) | bool %}`. When `deploy_dcgm=false`,
`prometheusSpec` is completely absent from the rendered file. This prevents
a nil pointer crash in the Helm chart template that occurs when `prometheusSpec`
is present but empty.

**Pre-flight Helm status check**
Before `helm upgrade --install` runs, the role checks the release status.
If the release is in `pending-install`, `pending-upgrade`, or `pending-rollback`
state (caused by a previous run being killed mid-upgrade), the play fails
immediately with a human-readable message instead of Helm's cryptic
"another operation in progress" error.

**Configurable nodeSelector**
The DCGM DaemonSet uses `dcgm_node_selector_key` / `dcgm_node_selector_value`
variables instead of a hardcoded label. The label `nvidia.com/gpu.present=true`
was written by old GFD (NVDP < 0.14) but was dropped in newer versions.
The default `nvidia.com/gpu.mode=compute` is written by all modern GFD versions.

---

## 11. Configuration reference

### group_vars/all.yml

| Variable | Default | Description |
|---|---|---|
| `monitoring_namespace` | `monitoring` | Kubernetes namespace for all components |
| `grafana_nodeport` | `32300` | NodePort for Grafana service |
| `prometheus_nodeport` | `32301` | NodePort for Prometheus service |
| `dcgm_namespace` | `monitoring` | Namespace for DCGM Exporter |
| `dcgm_metrics_port` | `9400` | Port DCGM Exporter serves metrics on |
| `dcgm_node_selector_key` | `nvidia.com/gpu.mode` | Node label key to identify GPU nodes |
| `dcgm_node_selector_value` | `compute` | Node label value to match |

### Runtime variables (injected by launcher, do not set in group_vars)

| Variable | Example | Description |
|---|---|---|
| `chart_version` | `65.1.0` | kube-prometheus-stack Helm chart version |
| `dcgm_version` | `3.3.5-3.4.0-ubuntu22.04` | DCGM Exporter image tag |
| `deploy_dcgm` | `true` / `false` | Controls DCGM play and values rendering |

### Choosing the right nodeSelector for your cluster

Run this on `ansiblecplane` to see all labels GFD has written:

```bash
kubectl get node <gpu-node-name> --show-labels | tr ',' '\n' | grep nvidia
```

Pick a label that:
- Has a fixed string value (not a timestamp or counter)
- Is present on GPU nodes
- Is absent on CPU-only nodes

| GFD version | Recommended label |
|---|---|
| NVDP >= 0.14 (modern) | `nvidia.com/gpu.mode=compute` |
| NVDP >= 0.14 alternative | `nvidia.com/mig.capable=false` |
| NVDP < 0.14 (old) | `nvidia.com/gpu.present=true` |

---

## 12. Concurrency protection

Running two monitoring installs concurrently causes Helm to fail with
"another operation (install/upgrade/rollback) is in progress".

Protection is two-layered:

**Frontend** (`templates/tabs/monitoring.html`)
`setMonitoringActionsDisabled(true)` is called at the start of either stream.
Both the "Install / Upgrade" and "Deploy DCGM Exporter" buttons are disabled
for the full duration. They are re-enabled together only on `__DONE__`,
`__ERROR__:LOCKED`, or `__ERROR__:<N>`.

**Backend** (`routes/monitoring.py`)
`_monitoring_lock = threading.Lock()` is shared across both SSE routes.
If the lock is already held when a new stream request arrives, the generator
immediately emits `data: __ERROR__:LOCKED` and exits. The stream opens
normally (HTTP 200) so the frontend `onmessage` handler receives the token —
there is no HTTP 409 on these routes.

The frontend checks `e.data === '__ERROR__:LOCKED'` exactly (not a substring
search) so the contract is stable and cannot be accidentally triggered by
Ansible log output.

---

## 13. Troubleshooting

### "another operation (install/upgrade/rollback) is in progress"

A previous Helm operation was interrupted and left the release in a pending state.

1. Check the release state:
   ```bash
   helm history kube-prometheus-stack -n monitoring
   helm list -n monitoring -a
   ```
2. If the last revision shows `pending-*` and nothing is currently running:
   ```bash
   kubectl get secrets -n monitoring -l owner=helm,name=kube-prometheus-stack
   kubectl patch secret <stuck-secret-name> -n monitoring \
     --type=merge -p '{"metadata":{"labels":{"status":"failed"}}}'
   ```
3. Re-run the install from the launcher.

### DCGM badge shows "⚠ deployed · no GPU nodes found"

The DaemonSet exists but `desiredNumberScheduled=0` — no node matches
`dcgm_node_selector_key=dcgm_node_selector_value`.

1. Check what labels exist on the GPU node:
   ```bash
   kubectl get node <gpu-node> --show-labels | tr ',' '\n' | grep nvidia
   ```
2. Check the current nodeSelector the DaemonSet is using:
   ```bash
   kubectl get daemonset dcgm-exporter -n monitoring \
     -o jsonpath='{.spec.template.spec.nodeSelector}'
   ```
3. If the label is missing or different, update `group_vars/all.yml`:
   ```yaml
   dcgm_node_selector_key:   nvidia.com/gpu.mode
   dcgm_node_selector_value: "compute"
   ```
4. Re-run the DCGM install from the launcher.

### DCGM badge shows "not installed" after a successful deploy

The launcher's `dcgm_status` endpoint queries `kubectl get daemonset dcgm-exporter`.
If this returns a non-zero exit code, it reports `not_installed`. Check:
```bash
kubectl get daemonset dcgm-exporter -n monitoring
kubectl get pods -n monitoring -l app=dcgm-exporter -o wide
```

### Grafana NVIDIA dashboard shows "No data"

The dashboard exists but Prometheus has no DCGM metrics yet.

1. Confirm the DCGM pod is running: `kubectl get pods -n monitoring -l app=dcgm-exporter`
2. Check Prometheus targets: open `http://<cp-ip>:32301/targets` and look for
   the `dcgm-exporter` job. It should show state UP.
3. If the job is missing, the values.yaml was rendered without the
   `additionalScrapeConfigs` block. Re-run the DCGM install (not just base install).
4. If the job shows DOWN, the DCGM pod IP is unreachable from Prometheus.
   Check Calico network policy and pod CIDR routing.

### Helm upgrade fails with "nil pointer evaluating prometheusSpec"

The `prometheusSpec` key was rendered as empty (present but null) in the
values file. This happens with an old version of `values.yaml.j2`. The fix
is to ensure `prometheusSpec` is inside the `{% if deploy_dcgm %}` block,
not outside it. Check `/tmp/monitoring-values.yaml` after rendering:
```bash
cat /tmp/monitoring-values.yaml | grep -A2 prometheusSpec
```
If `prometheusSpec:` appears with no children when `deploy_dcgm=false`,
the template is outdated.

### "AnsibleLookupError: file not found" for my-dashboard.json

A Jinja2 `{{ lookup('file', '...') }}` expression exists inside a comment
in `values.yaml.j2`. Ansible evaluates all `{{ }}` expressions including
those in comments. Wrap documentation-only examples in `{% raw %}...{% endraw %}`.