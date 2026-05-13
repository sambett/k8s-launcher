# AI Workbench Monitor Dashboard Documentation

## Purpose

This document explains the `AI Workbench Monitor3` Grafana dashboard exported in [workbench-dashboard.json](C:/Users/SelmaB/Desktop/workbench-dashboard.json). It is intended to help administrators, operators, and collaborators understand what each dashboard section shows, why it exists, and when it is useful.

The dashboard brings together observability for:

- GPU health and utilization
- Kubernetes node CPU and memory usage
- JupyterHub notebook activity
- Per-user resource consumption
- Longhorn storage capacity and throughput

This combination is important because the workbench is not a single service. It is an operating environment where compute, notebooks, users, and storage all affect one another. A useful dashboard therefore needs to show both infrastructure health and user workload behavior in one place.

## Who This Dashboard Is For

- Platform administrators maintaining the Kubernetes workbench
- Operators troubleshooting cluster health and performance
- JupyterHub administrators monitoring notebook activity
- Research or engineering leads who need visibility into resource pressure

## Global Utility of the Dashboard

This dashboard is necessary because it answers the main operational questions behind a shared AI workbench:

- Are GPUs available, healthy, and being used correctly?
- Are cluster nodes under CPU or RAM pressure?
- Are user notebooks running normally or failing to start?
- Which users or pods are consuming the most resources?
- Is Longhorn storage healthy, schedulable, and large enough for workloads?

Without a consolidated view like this, troubleshooting becomes slow and fragmented across multiple dashboards and systems.

## Filters and Scope

The dashboard includes two main filters:

- `Node`: limits views to one or more Kubernetes nodes
- `User`: limits user-related notebook views to one or more JupyterHub users

These filters are useful because they let operators move between global monitoring and targeted troubleshooting without changing dashboards.

## 1. GPU Monitoring

### Utility

This section gives a quick health summary of the GPU estate. It is designed for immediate situational awareness: how many GPUs are visible, how much power they are drawing, how hot they are running, and how much memory is available.

### Why It Is Necessary

GPU nodes are usually the most valuable and constrained part of an AI platform. If GPUs overheat, disappear, or run out of memory, user workloads degrade quickly. This section helps teams detect early warning signs before notebook performance collapses.

### Panels

#### GPU Count

Shows the number of GPUs currently visible from monitoring data.

Why it matters:

- Confirms that GPU exporters are reporting correctly
- Helps detect missing devices after node issues, driver failures, or exporter problems

#### Total GPU Power

Shows the combined GPU power draw across the selected nodes.

Why it matters:

- Indicates whether GPUs are active or mostly idle
- Helps correlate performance issues with actual hardware load
- Can reveal suspicious underuse or unusually high sustained activity

#### Avg GPU Temperature

Shows the average GPU temperature.

Why it matters:

- High temperature is an early sign of cooling or workload issues
- Sustained thermal pressure can reduce performance or threaten stability

#### GPU Memory Used (avg)

Shows average GPU memory consumption.

Why it matters:

- Helps determine whether workloads are memory-bound
- Useful for validating whether GPU-backed notebooks are consuming expected VRAM

#### GPU Free Memory (avg)

Shows average remaining GPU memory.

Why it matters:

- Helps estimate whether new jobs can be scheduled safely
- Low free memory can explain OOM conditions or failed GPU notebook starts

## 2. GPU Utilisation

### Utility

This section moves from static GPU health into workload behavior over time. It shows how hard GPUs are working and how much memory individual GPU models are consuming.

### Why It Is Necessary

A GPU being present is not the same as a GPU being used efficiently. This section helps distinguish between availability, real utilization, and wasted capacity.

### Panels

#### GPU Utilisation % by Model

Shows GPU utilization over time, grouped by node, model, and GPU index.

Why it matters:

- Reveals whether GPUs are actively computing or mostly idle
- Helps spot imbalanced usage across nodes or GPU models
- Useful when validating scheduler behavior and workload placement

#### GPU Memory Used by Model (MiB)

Shows VRAM consumption over time by node, model, and GPU index.

Why it matters:

- Identifies memory-heavy workloads
- Helps detect fragmentation or persistent allocation pressure
- Useful for choosing the right notebook profiles and GPU sizing

## 3. CPU and RAM by Node

### Utility

This section focuses on general node health. Even in GPU-heavy platforms, CPU and system memory remain critical because Kubernetes services, notebook kernels, monitoring agents, and storage components all depend on them.

### Why It Is Necessary

Many platform issues that appear to be "GPU problems" are actually node pressure problems. High CPU or RAM use can affect scheduling, notebook responsiveness, and cluster stability long before a node is formally marked unhealthy.

### Panels

#### Current CPU Usage % per Node

Shows the latest CPU usage for each node.

Why it matters:

- Quickly identifies overloaded nodes
- Helps balance workloads and detect noisy neighbors

#### Current RAM Usage % per Node

Shows the latest memory pressure for each node.

Why it matters:

- High RAM pressure can cause eviction, instability, or slow notebooks
- Helps detect nodes approaching unsafe operating thresholds

#### CPU Utilisation over Time per Node

Shows CPU usage trends by node.

Why it matters:

- Distinguishes temporary spikes from sustained saturation
- Helps correlate incidents with workload timing

#### RAM Consumption over Time per Node

Shows memory consumption trends by node.

Why it matters:

- Useful for detecting leaks, gradual buildup, or long-running pressure
- Helps with capacity planning and node right-sizing

## 4. JupyterHub Sessions

### Utility

This section shows the live state of notebook pods in the `jhub` namespace. It focuses on whether notebooks are running, waiting, restarting, or failing.

### Why It Is Necessary

The platform exists primarily to serve notebook users. Infrastructure metrics alone are not enough if users cannot start or keep notebooks running. This section connects platform health to actual user experience.

### Panels

#### Running Notebooks

Shows how many notebook pods are currently running.

Why it matters:

- Gives a direct signal of active platform usage
- Helps estimate real-time demand on compute and storage

#### Pending Notebooks

Shows notebook pods that are waiting to schedule or start.

Why it matters:

- A key early warning for resource shortages, scheduling conflicts, or image pull issues
- Helpful for identifying friction before users report it

#### Notebooks with Restarts (1h)

Shows notebooks that restarted within the last hour.

Why it matters:

- Restarts often indicate instability, crashes, or resource exhaustion
- Helps catch unreliable user environments quickly

#### Failed Notebooks

Shows notebook pods that are in a failed state.

Why it matters:

- Directly signals broken user sessions
- Important for escalation, incident response, and user support

## 5. Per-User Resource Usage

### Utility

This section attributes consumption to individual notebook pods and users. It answers who is using what, rather than only showing cluster totals.

### Why It Is Necessary

In multi-user AI platforms, fairness and accountability matter. Without per-user visibility, it is difficult to investigate noisy-neighbor behavior, justify capacity growth, or support users effectively.

This section is especially valuable for:

- spotting heavy users
- explaining performance contention
- validating notebook profile choices
- understanding GPU allocation behavior per notebook

### Panels

#### Top RAM Users Now

Shows which notebook users are consuming the most memory at the moment.

Why it matters:

- Helps identify memory-heavy sessions quickly
- Useful during node pressure or eviction investigations

#### Top CPU Users Now (cores)

Shows which notebook users are consuming the most CPU right now.

Why it matters:

- Helps detect hot workloads and unfair contention
- Useful for diagnosing slow shared-node performance

#### RAM per User over Time

Shows memory usage trends for user notebooks.

Why it matters:

- Helps distinguish short spikes from sustained heavy usage
- Useful for leak detection and capacity review

#### CPU per User over Time (cores)

Shows CPU usage trends for user notebooks.

Why it matters:

- Helps identify long-running compute workloads
- Useful when deciding whether users should move to different profiles or nodes

#### GPU VRAM per User (MiB)

Shows GPU memory used by notebook pods with GPU allocation.

Why it matters:

- Confirms which users are actually consuming GPU memory
- Helps explain GPU scarcity or failed GPU notebook starts

#### GPU Utilisation per User

Shows GPU activity per notebook pod.

Why it matters:

- Distinguishes between reserved GPUs and actively used GPUs
- Helps identify idle but allocated GPU sessions

### Important Interpretation Note

GPU panels in this section only show pods with active GPU allocation. CPU-only notebooks are expected to show no GPU data. This is correct behavior and should not be treated as a monitoring gap.

## 6. Longhorn Storage

### Utility

This section monitors the health, capacity, and throughput of Longhorn-backed storage. Because notebook environments depend on persistent volumes, storage health is a first-class operational concern.

### Why It Is Necessary

Even if compute is healthy, user workflows fail when persistent storage is full, degraded, or unschedulable. This section helps operators protect notebook persistence, data access, and platform reliability.

### Panels

#### Storage Free %

Shows the percentage of free storage remaining across Longhorn disks.

Why it matters:

- Provides an immediate capacity health signal
- Helps prevent platform-wide storage exhaustion

#### Disk Usage % Fullest First

Shows how full individual disks are.

Why it matters:

- Helps detect imbalance across nodes or disks
- Useful for spotting the next disk likely to become a bottleneck

#### Ready Disks

Shows how many Longhorn disks are in a ready state.

Why it matters:

- Confirms whether storage devices are operational
- A drop can indicate node, mount, or disk-level problems

#### Schedulable Disks

Shows how many disks can still accept scheduled storage workloads.

Why it matters:

- Important for volume placement and future notebook provisioning
- Helps distinguish healthy disks from merely visible ones

#### Non-Schedulable

Shows the number of disks that cannot currently accept workloads.

Why it matters:

- A direct operational risk indicator
- Important for early intervention before storage becomes constrained

#### Total Capacity

Shows total available Longhorn disk capacity.

Why it matters:

- Useful for baseline capacity planning
- Helps teams understand total platform storage scale

#### Total Used

Shows total used Longhorn storage.

Why it matters:

- Helps track storage growth over time
- Useful for identifying when cleanup or expansion is needed

#### Disk Usage over Time

Shows storage consumption trends by node and disk.

Why it matters:

- Useful for growth analysis and anomaly detection
- Helps find which disks are filling fastest

#### Longhorn Storage I/O Total Write and Read Throughput

Shows aggregate Longhorn read and write throughput over time.

Why it matters:

- Helps identify storage-intensive notebook workloads
- Useful for diagnosing slow I/O or backend contention

## Operational Value Summary

This dashboard is valuable because it combines the full lifecycle of AI workbench operations in one place:

- infrastructure health
- user session state
- per-user accountability
- GPU allocation and efficiency
- persistent storage reliability

The dashboard is not just informative; it is operationally necessary for day-to-day support, incident response, and capacity planning in a shared GPU-enabled JupyterHub platform.

## Recommended Use

The dashboard is most effective when used in these modes:

- Daily health review by platform administrators
- Incident triage when users report slow or failed notebooks
- Capacity planning for GPU, RAM, and storage growth
- Post-incident review to understand whether compute, notebook, or storage pressure was the root cause

## Source

- Dashboard export: [workbench-dashboard.json](C:/Users/SelmaB/Desktop/workbench-dashboard.json)
