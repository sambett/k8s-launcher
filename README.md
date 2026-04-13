
# k8s-launcher

A browser-based deployment tool for provisioning a production-grade Kubernetes cluster on bare Ubuntu VMs — no prior Ansible or Kubernetes knowledge required.

You fill in a form, click Deploy, and watch the cluster build itself in real time. The launcher handles SSH setup, Ansible installation, inventory generation, Kubernetes initialization, CNI networking, and distributed storage — all streamed live to your browser.

Once the cluster is running, it hosts **JupyterHub**: a multi-user AI notebook platform where each user gets an isolated environment with persistent storage, configurable CPU/RAM profiles, and optional GPU access — all managed through a separate admin dashboard without ever touching the terminal.

---

## What gets deployed

| Component | Role |
|---|---|
| Kubernetes 1.30.5 | Container orchestration |
| Calico CNI | Pod networking |
| Longhorn 1.7.2 | Persistent distributed storage |
| GitLab CE | User authentication + container registry |
| JupyterHub | Multi-user AI notebook portal |
| Kyverno | GPU policy enforcement |
| Admin Dashboard | Browser-based Day-2 management |

---

## VM Requirements

- All nodes must run **Ubuntu 22.04 LTS** or **Ubuntu 24.04 LTS**
- All nodes must run the **same OS version** — mixed OS clusters are not supported
- Architecture: **amd64 (x86-64)** only
- Minimum per node: 2 vCPU, 4 GB RAM, 40 GB disk
- All VMs must be on the same network and reachable on port 22

---

## Prerequisites — Before Launching the Platform

The following must be done **manually, once**, on every VM before starting the launcher. The platform cannot automate these steps because it has no credentials yet.

### 1 — Install the launcher dependencies on the controller VM

The controller is the machine that will run the launcher. It must **not** be one of the cluster nodes.

```bash
sudo apt update && sudo apt install -y python3-pip git openssh-client
pip3 install fastapi uvicorn paramiko pyyaml ansible

git clone https://github.com/sambett/k8s-launcher.git
cd k8s-launcher
python3 app.py
```

### 2 — Make all VMs reachable via SSH with no password

From the **controller**, push your key to every node:

```bash
# Generate a key pair on the controller if you don't have one
ls ~/.ssh/id_ed25519 || ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""

# Push it to each node — you will be asked for the password once per node
ssh-copy-id cplane@<cplane-ip>
ssh-copy-id worker01@<worker01-ip>
ssh-copy-id worker02@<worker02-ip>
# Repeat for any additional nodes and the GitLab VM if applicable
```

Record all fingerprints in `known_hosts` so OpenSSH never prompts:

```bash
ssh-keyscan -H <cplane-ip> <worker01-ip> <worker02-ip> >> ~/.ssh/known_hosts
```

### 3 — Grant passwordless sudo on every node

The platform runs Ansible with `become: yes` — every node must allow sudo without a password. Run this once per node, replacing the username and IP:

```bash
ssh -t cplane@<cplane-ip> \
  "echo 'cplane ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ansible-nopasswd \
   && sudo chmod 440 /etc/sudoers.d/ansible-nopasswd"

ssh -t worker01@<worker01-ip> \
  "echo 'worker01 ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ansible-nopasswd \
   && sudo chmod 440 /etc/sudoers.d/ansible-nopasswd"

ssh -t worker02@<worker02-ip> \
  "echo 'worker02 ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/ansible-nopasswd \
   && sudo chmod 440 /etc/sudoers.d/ansible-nopasswd"
```

### 4 — Verify everything is ready

All three commands below should print `root` with **no password prompt**:

```bash
ssh cplane@<cplane-ip>    "sudo whoami"
ssh worker01@<worker01-ip> "sudo whoami"
ssh worker02@<worker02-ip> "sudo whoami"
```

If all three return `root` instantly, your VMs are ready. The launcher will take it from here.

> **Note:** The Bootstrap tab inside the launcher automates steps 2 and 3 for you
> if you prefer to let the platform handle it. You only need to do this manually
> if you want to pre-trust the nodes before opening the UI.

---

## Open the launcher

Once the launcher is running on the controller: http://<controller-ip>:5000
---



## Usage

The launcher walks you through the following tabs in order:

### Bootstrap
Enter the IP, SSH user, and password for each node. The launcher pushes the
controller SSH key, configures passwordless sudo, and records all fingerprints
automatically. After this step no password is ever needed again.

### Configure
Enter node roles, Kubernetes version, network CIDRs, and Longhorn settings.
The launcher validates version compatibility and writes the Ansible inventory.
It also wires up passwordless SSH from the control plane to all workers
automatically.

### Kubernetes
Click **Deploy** and watch the installation stream live. The launcher runs
Ansible to install containerd, kubeadm, initialize the control plane, join
workers, deploy Calico CNI and Longhorn storage.
Expected time: **15–20 minutes**.

### GitLab
Deploy GitLab CE as the identity provider and container registry. Step 1
bootstraps SSH trust to the GitLab VM the same way the Bootstrap tab does
for cluster nodes.

### JupyterHub
Deploy JupyterHub backed by GitLab OAuth. Users log in with their GitLab
credentials and get isolated notebook environments with persistent storage.

### Kyverno
Deploy and manage GPU enforcement policies. Controls which GitLab groups
can request GPU resources and sets per-group limits.

### Workers
Add or remove worker nodes from a running cluster. Adding a worker
bootstraps full SSH trust automatically before joining it — no manual
preparation needed for new nodes once the initial cluster is running.

---

## Admin dashboard

After a full deployment, a browser-based admin dashboard is available on
the control plane node at port **8888**. It provides:

- **Profiles** — create and manage JupyterHub notebook environments
- **Groups** — manage GitLab groups and JupyterHub access
- **Users** — browse and assign GitLab users to groups
- **Images** — import notebook Docker images into the GitLab registry
- **GPU Policies** — set per-group GPU type and usage limits enforced by Kyverno

---

## Standalone Ansible usage

The Ansible projects inside this repo are fully self-contained:

```bash
# Deploy Kubernetes
cd ansible-k8s
ansible-playbook -i inventory/hosts.yml site.yml

# Deploy Longhorn
cd ansible-longhorn
ansible-playbook -i inventory/hosts.yml site.yml
```

Edit `inventory/hosts.yml` and `group_vars/all.yml` in each project to match
your environment.

---

## Authors

Sam Bettaieb · Selma
Final-year engineering thesis (PFE) — on-premise AI Workbench as a Service
EOF
