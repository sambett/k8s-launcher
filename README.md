# k8s-launcher

Deploy a production Kubernetes cluster from your browser.
No Ansible or Kubernetes knowledge required.

---

## What this project does

You have several fresh Ubuntu virtual machines.
You run three commands on one of them.
You open a browser and click through a dashboard.
You end up with a fully working Kubernetes cluster with distributed
block storage (Longhorn), ready for workloads.

Everything — SSH setup, Ansible, cluster deployment, storage — is
handled by the dashboard. You never need to touch a terminal on the
cluster machines.

---

## What gets deployed

| Component | Version | Purpose |
|---|---|---|
| Kubernetes | 1.30.5 | Container orchestration |
| containerd | 1.7.22 | Container runtime |
| Calico | v3.28.2 | Pod networking (CNI) |
| Longhorn | 1.7.2 | Distributed block storage |

---

## What you need before starting

### Machines

You need at least 3 Ubuntu VMs. 4 is recommended.

| Role | Count | Min RAM | Min Disk | OS |
|---|---|---|---|---|
| Controller | 1 | 1 GB | 10 GB | Ubuntu 22.04 or 24.04 |
| Control plane | 1 | 2 GB | 20 GB | Ubuntu 22.04 or 24.04 |
| Worker | 1–N | 2 GB | 20 GB | Ubuntu 22.04 or 24.04 |

The **controller** is where you run the launcher. It is not part of
the Kubernetes cluster — it just manages the other machines.

All machines must be able to reach each other over the network via SSH.

### What you need to know about each machine

Before starting, collect these for every machine:

- IP address
- Hostname (the machine's name, e.g. `ansiblecplane`)
- SSH username (the user you log in as)
- SSH password

You will enter these into the dashboard during setup.

### Software on the controller only

Python 3.8 or higher. Check with:

```bash
python3 --version
```

If not installed:

```bash
sudo apt update && sudo apt install -y python3 python3-pip
```

---

## Setup — three commands

SSH into the **controller VM**:

```bash
ssh youruser@<controller-ip>
```

Then run:

```bash
# 1. Get the project
git clone https://github.com/sambett/k8s-launcher
cd k8s-launcher

# 2. Install launcher dependencies
pip3 install -r requirements.txt

# 3. Make sure pip-installed tools are on PATH (required once per session)
export PATH=$PATH:~/.local/bin

# 4. Start the launcher
python3 launcher.py
```

You should see:

```
INFO:     Uvicorn running on http://0.0.0.0:5000
INFO:     Application startup complete.
```

Leave this terminal open and running.

Open your browser and go to:

```
http://<controller-ip>:5000
```

> Your browser must be able to reach the controller IP.
> If you are on the same network as the VMs, this will work directly.
> If not, you may need to use an SSH tunnel:
> `ssh -L 5000:localhost:5000 youruser@<controller-ip>`
> then open `http://localhost:5000`

---

## Dashboard walkthrough

### Tab 1 — Bootstrap

This tab sets up everything needed before deployment can run.

**Step 1 — Install Ansible**

Click **Install / verify Ansible**.

The launcher checks if Ansible is installed on the controller.
If not, it installs it automatically. You will see a green badge
with the installed version when done.

**Step 2 — SSH key bootstrap**

The dashboard shows three node rows pre-filled with example IPs.
Replace them with your actual node details:

- IP address of the node
- Hostname of the node
- SSH username
- SSH password

Click **+ Add node** to add more rows. Click ✕ to remove one.

Click **Bootstrap SSH**.

The launcher connects to each node once using the password you entered,
pushes an SSH key, and from that point forward connects without a
password. Passwords are never stored — they are used once and discarded.

You will see a green badge for each node when done.

**Step 3 — Preflight checks**

Click **Run preflight**.

The launcher checks every node for:
- SSH connectivity
- OS version (must be Ubuntu 22.04 or 24.04)
- Available RAM (minimum 1.8 GB)
- Free disk space (minimum 20 GB)
- Swap disabled (required by Kubernetes)
- Python 3 installed
- No previous cluster state

All checks must show ✓ before proceeding.

**If a check fails:**

| Check | Fix |
|---|---|
| swap FAIL | `sudo swapoff -a` on that node |
| python3 FAIL | `sudo apt install -y python3` on that node |
| kubeadm_state FAIL | A cluster already exists on this node — reset it first |
| disk FAIL | Free up space or use a larger disk |

After fixing, click Run preflight again.

---

### Tab 2 — Configure

Fill in your cluster topology.

**Control plane** — enter the IP, hostname, and SSH user of the
control plane machine.

**Worker nodes** — the rows are pre-filled with examples. Replace
with your actual worker IPs and hostnames. Use **+ Add worker** to
add more workers. The launcher supports any number.

**Versions** — pre-filled with the validated stack. Change only if
you know what you are doing. Incompatible combinations show a warning
instantly.

**Components** — check or uncheck Longhorn storage.

Click **Generate configuration**.

The launcher creates two files:
- `inventory.ini` — list of all your nodes
- `group_vars/all.yml` — all cluster settings

Both files are shown in preview panels below the button.
Review them before deploying.

> **Longhorn replicas are set automatically:**
> 1 worker → 1 replica, 2 workers → 2 replicas, 3+ → 3 replicas.
> You never need to configure this.

---

### Tab 3 — Deploy

Run the deployment phases in order. Do not skip steps.

**Phase 4 — Deploy Kubernetes**

Click **Deploy Kubernetes**.

Live Ansible output streams into the log panel line by line.
You can watch every task as it runs.

Expected duration: **10–15 minutes** depending on network speed.

Wait for `__DONE__` to appear at the bottom of the log.
If `__ERROR__` appears, read the lines above it to find the
failing task. Fix the issue and click Deploy again — it is safe
to rerun.

**Phase 5 — Validate Kubernetes**

Click **Validate Kubernetes**.

Checks: all nodes Ready, CoreDNS running, node count correct,
kubeconfig saved. All must show ✓.

**Phase 6 — Deploy Longhorn**

Click **Deploy Longhorn**.

Expected duration: **5–8 minutes**.

Wait for `__DONE__`.

**Phase 7 — Validate Longhorn**

Click **Validate Longhorn**.

Checks: all pods Running, StorageClass exists, both workers
schedulable, UI accessible. All must show ✓.

---

### Tab 4 — Status

Your cluster is running. This tab gives you everything you need.

**Validation results** — full detail from the last validate run.

**Add worker node** — scale your cluster up without a terminal.
Enter the new node's details and click **Add worker**.
The launcher pushes SSH keys, runs the join command, and labels
the node automatically. The new node appears in the cluster within
about a minute.

**Join token** — click **Show join token** to see the kubeadm join
command. The token expires after 24 hours.

To regenerate a token manually (on the control plane):
```bash
kubeadm token create --print-join-command
```

**Longhorn UI** — click the link to open the Longhorn storage
dashboard in a new tab.

**kubeconfig** — click **Download kubeconfig.yaml** and copy it
to your local machine:

```bash
mkdir -p ~/.kube
cp ~/Downloads/kubeconfig.yaml ~/.kube/config
kubectl get nodes
```

Expected output:
```
NAME             STATUS   ROLES           AGE   VERSION
ansiblecplane    Ready    control-plane   Xm    v1.30.5
ansibleworker1   Ready    worker          Xm    v1.30.5
ansibleworker2   Ready    worker          Xm    v1.30.5
```

---

## If something goes wrong

**Launcher does not start — port in use:**
```bash
pkill -f "python3 launcher.py"
python3 launcher.py
```

**Browser cannot reach the dashboard:**
Check the controller IP is correct. If the controller is on a
remote network, use an SSH tunnel:
```bash
ssh -L 5000:localhost:5000 youruser@<controller-ip>
```
Then open `http://localhost:5000`.

**Deployment fails mid-run:**
Read the log — the failing task name is always shown.
Fix the issue, click Deploy again. The playbooks are idempotent —
completed tasks are skipped automatically.

**SSH bootstrap fails — authentication error:**
The password you entered is wrong. Verify it by connecting manually:
```bash
ssh youruser@<node-ip>
```

**Preflight shows kubeadm_state FAIL on control plane:**
A cluster already exists. This is expected if you are reusing
machines. Reset it first:
```bash
ssh youruser@<control-plane-ip>
sudo kubeadm reset -f
sudo rm -rf /etc/kubernetes /var/lib/etcd ~/.kube
```

---

## Project structure

```
k8s-launcher/
├── launcher.py          entry point — starts the web server
├── requirements.txt     Python dependencies
├── compat_matrix.json   version compatibility rules
├── core/
│   ├── paths.py         all file path constants
│   └── ssh.py           SSH connection helpers
├── routes/
│   ├── bootstrap.py     install Ansible + push SSH keys
│   ├── preflight.py     node readiness checks
│   ├── configure.py     generate inventory + variables
│   ├── deploy.py        run playbooks + validate + add worker
│   └── status.py        kubeconfig download
├── templates/
│   └── index.html       complete dashboard (single HTML file)
├── ansible-k8s/         Kubernetes deployment playbooks
└── ansible-longhorn/    Longhorn deployment playbooks
```

---

## Architecture

```
Your browser
      │
      │ HTTP  http://<controller-ip>:5000
      ▼
┌─────────────────────────────────────┐
│ Controller VM                       │
│ launcher.py + Ansible               │
└──────────┬──────────────────────────┘
           │ SSH (passwordless)
     ┌─────┼───────────┐
     ▼     ▼           ▼
  cp VM  worker1  worker2 ...
```

The controller is intentionally outside the cluster.
If the cluster has issues, the launcher stays available.

---

## Validated on

Ubuntu 22.04 LTS · Kubernetes 1.30.5 · Calico v3.28.2 · Longhorn 1.7.2
