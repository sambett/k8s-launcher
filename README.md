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

## Requirements

- One Ubuntu 22.04 VM to act as the **launcher host** (Ansible controller)
- Two or more fresh Ubuntu 22.04 VMs for the **cluster nodes**
- SSH access (username + password) from the launcher host to all nodes
- Python 3.10+ on the launcher host
- All VMs on the same network

---

## Install

Run the following on your **launcher host**:
```bash
sudo apt update && sudo apt install -y python3-pip git
pip3 install fastapi uvicorn paramiko pyyaml ansible

git clone https://github.com/sambett/k8s-launcher.git
cd k8s-launcher

python3 app.py
```

---

## Open the launcher

Once running, open your browser and go to:
http://<launcher-host-ip>:5000

---

## Usage

The launcher walks you through 5 steps:

### 1. Bootstrap
Enter the IP address and SSH credentials for each node. The launcher generates an SSH key pair and distributes it to all machines so Ansible can connect without a password going forward.

### 2. Configure
Enter the number of worker nodes, select the Kubernetes version, set the pod network CIDR, and choose the Longhorn replica count. The launcher validates version compatibility before letting you proceed.

### 3. Deploy
Click **Deploy** and watch the installation stream live to your browser. The launcher runs two Ansible projects in sequence:
- `ansible-k8s` — installs containerd, kubeadm, initializes the control plane, joins workers, deploys Calico
- `ansible-longhorn` — installs Helm, deploys Longhorn, sets the default StorageClass

Expected time: **15–20 minutes** on a fresh set of VMs.

### 4. Status
Once deployment completes, this tab shows the health of every component. You can also generate a fresh worker join token and download your `kubeconfig` file to manage the cluster from your local machine.

### 5. Reset
Safely wipe the cluster. The launcher shows you exactly which nodes will be affected before you confirm. Use this to start over on the same machines.

---

## Adding a worker node after deployment

In the **Status** tab, use the **Add Worker** button. Enter the new node's IP — the launcher installs all prerequisites and joins it to the existing cluster automatically.

---

## Admin dashboard

After a full deployment, a browser-based admin dashboard is available on the control plane node at port **8888**. It provides:

- **Profiles** — create and manage JupyterHub notebook environments (CPU/RAM/GPU limits, notebook image)
- **Groups** — manage GitLab groups, which control which profiles each user can access
- **Users** — browse and assign GitLab users to groups
- **Images** — import notebook Docker images into the GitLab container registry
- **GPU Policies** — set per-group GPU type and usage limits, enforced at the cluster level by Kyverno

Profile changes take effect within ~60 seconds — no Helm upgrades, no service restarts, no user interruption.

---

## Standalone Ansible usage

The two Ansible projects inside this repo are fully self-contained and can be used without the launcher:
```bash
# Deploy Kubernetes
cd ansible-k8s
ansible-playbook -i inventory/hosts.yml site.yml

# Deploy Longhorn
cd ansible-longhorn
ansible-playbook -i inventory/hosts.yml site.yml
```

Edit `inventory/hosts.yml` and `group_vars/all.yml` in each project to match your environment.

---

## Authors

Sam Bettaieb · Selma  
Final-year engineering thesis (PFE) — on-premise AI Workbench as a Service
