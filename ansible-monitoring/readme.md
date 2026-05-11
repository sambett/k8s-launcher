# ansible-monitoring

Deploys the observability stack for the AI Workbench platform.

---

## Architecture overview

The monitoring stack has two distinct layers. Understanding which layer does what
prevents confusion when debugging or extending it.

### Layer 1 — kube-prometheus-stack (Helm chart, always installed)

`kube-prometheus-stack` is a community Helm chart that bundles the entire
Kubernetes monitoring platform into a single install. We do **not** build
these components ourselves — we let Helm manage them.

| Component | What it does |
|---|---|
| **Prometheus** | Scrapes metrics from all configured targets, stores them as time-series data, and answers PromQL queries |
| **Alertmanager** | Receives alerts from Prometheus, deduplicates and routes them (e-mail, Slack, etc.) |
| **Grafana** | Visualisation UI — reads from Prometheus and renders dashboards |
| **Prometheus Operator** | Kubernetes controller that manages Prometheus and Alertmanager as CRDs (`PrometheusRule`, `ServiceMonitor`, etc.) |
| **kube-state-metrics** | Exposes Kubernetes object state as metrics (pod counts, deployment replicas, node conditions, etc.) |
| **node-exporter** | DaemonSet on every node — exposes OS-level metrics (CPU, RAM, disk, network) |

Our Ansible role (`prometheus_stack`) does three things:
1. Ensures the `monitoring` namespace exists
2. Renders `values.yaml.j2` to customise the chart (NodePorts, optional GPU config)
3. Calls `helm upgrade --install` to let the chart do the rest

### Layer 2 — DCGM Exporter (optional, GPU nodes only)

The base chart gives us cluster and node metrics, but **not GPU telemetry**.
NVIDIA GPU metrics require a separate component: [DCGM Exporter](https://github.com/NVIDIA/dcgm-exporter).

Our `dcgm_exporter` role deploys it as a Kubernetes DaemonSet that:
- Runs **only on nodes labelled** `nvidia.com/gpu.present=true`
  (this label is written by GPU Feature Discovery — GFD — automatically)
- Exposes GPU metrics on port `9400` via the `/metrics` endpoint
- Is scraped by Prometheus using an `additionalScrapeConfigs` entry we add to values

**GPU metrics exposed by DCGM Exporter:**

| Metric | Meaning |
|---|---|
| `DCGM_FI_DEV_GPU_UTIL` | GPU core utilisation % |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | GPU memory bandwidth utilisation % |
| `DCGM_FI_DEV_FB_USED` | Framebuffer memory used (MiB) |
| `DCGM_FI_DEV_FB_FREE` | Framebuffer memory free (MiB) |
| `DCGM_FI_DEV_GPU_TEMP` | GPU temperature (°C) |
| `DCGM_FI_DEV_POWER_USAGE` | Power draw (W) |
| `DCGM_FI_DEV_SM_CLOCK` | SM clock speed (MHz) |

---

## How GPU metrics reach Grafana (end-to-end flow)

```
GPU hardware
    │
    ▼
NVIDIA kernel driver  (reports metrics to DCGM daemon)
    │
    ▼
DCGM Exporter pod     (reads from DCGM daemon, serves /metrics on :9400)
    │
    ▼  (scraped every 15s via additionalScrapeConfigs in values.yaml.j2)
Prometheus            (stores metrics as time-series)
    │
    ▼  (PromQL queries)
Grafana               (renders dashboards — NVIDIA dashboard gnetId 12239)
    │
    ▼
Researcher browser    (NodePort 32300)
```

---

## Grafana dashboard provisioning

Grafana supports **automatic dashboard provisioning** — dashboards are declared
in configuration and imported at startup, with no manual UI clicking required.

In `values.yaml.j2` (when `deploy_dcgm=true`), we configure:

```yaml
grafana:
  dashboardProviders:        # Tells Grafana WHERE to look for JSON files
    dashboardproviders.yaml:
      providers:
        - name: nvidia        # Creates an "NVIDIA" folder in Grafana
          ...

  dashboards:
    nvidia:
      dcgm-exporter:
        gnetId: 12239         # Downloads this dashboard from grafana.com
        datasource: Prometheus
```

**What is `gnetId`?**  
Every dashboard published to [grafana.com/grafana/dashboards](https://grafana.com/grafana/dashboards)
has a numeric ID. Grafana can download and import it automatically at startup.
`gnetId: 12239` is the official NVIDIA DCGM Exporter dashboard.

**How to add a custom dashboard:**

Option A — reference by gnetId (for public Grafana.com dashboards):
```yaml
dashboards:
  nvidia:
    my-custom-gpu-dash:
      gnetId: <id from grafana.com>
      revision: 1
      datasource: Prometheus
```

Option B — version-control the JSON yourself (recommended for custom dashboards):
1. Build your dashboard in Grafana UI
2. Export it: Dashboard → Share → Export → Save to file
3. Save the JSON to `roles/prometheus_stack/files/my-dashboard.json`
4. Reference it in `values.yaml.j2`:
```yaml
dashboards:
  custom:
    my-dashboard:
      json: |
        {{ lookup('file', 'my-dashboard.json') | indent(8) }}
```

Option B gives you version history, peer review, and reproducible deploys.

---

## Install flows

### Base monitoring install
Triggered by the k8s-launcher Monitoring tab → "Install / Upgrade" button.

```
routes/monitoring.py  →  ansible_stream(extra_vars={deploy_dcgm: false})
    │
    ▼
ansible-monitoring/site.yml
    ├── play: prometheus_stack   (Helm install/upgrade)
    ├── play: dcgm_exporter      (SKIPPED — deploy_dcgm=false)
    └── play: validate           (checks readiness)
```

Values rendered: NodePorts only. No DCGM scrape config. No NVIDIA dashboard.

### DCGM / GPU monitoring install
Triggered by the k8s-launcher Monitoring tab → "Deploy DCGM Exporter" button.

```
routes/monitoring.py  →  ansible_stream(extra_vars={deploy_dcgm: true, dcgm_version: ...})
    │
    ▼
ansible-monitoring/site.yml
    ├── play: prometheus_stack   (Helm upgrade — adds DCGM scrape config + NVIDIA dashboard)
    ├── play: dcgm_exporter      (deploys DaemonSet on GPU nodes)
    └── play: validate           (checks readiness)
```

Values rendered: NodePorts + DCGM `additionalScrapeConfigs` + Grafana NVIDIA dashboard.

---

## Concurrency safety

Both install flows run `ansible-playbook` as a subprocess. Running two installs
concurrently causes Helm to error: `another operation (install/upgrade/rollback) is in progress`.

Protection is two-layered:
- **Frontend**: buttons are disabled for the duration of the SSE stream
- **Backend**: `_monitoring_lock` in `routes/monitoring.py` returns HTTP 409
  if a stream is already active

---

## Runtime variables

These are **not** in `group_vars/all.yml` — they are injected at runtime by k8s-launcher:

| Variable | Source | Example |
|---|---|---|
| `chart_version` | launcher UI input | `65.1.0` |
| `dcgm_version` | launcher UI input | `3.3.5-3.4.0-ubuntu22.04` |
| `deploy_dcgm` | launcher route | `true` or `false` |

---

## What is safe to customise

| File | Safe to change | Notes |
|---|---|---|
| `group_vars/all.yml` | NodePort values, namespace names | Changes take effect on next Helm upgrade |
| `values.yaml.j2` | Any Helm chart value | See chart docs at artifacthub.io |
| `dcgm_exporter/templates/dcgm.yaml.j2` | Resource limits, tolerations | Restart DaemonSet after changes |
| `compat_matrix.json` | Add new chart/DCGM versions | Keep existing entries — they're in Helm history |