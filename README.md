# k8s-launcher

Deploy a production Kubernetes cluster from your browser.
No Ansible or Kubernetes knowledge required.

---

## What it deploys

| Component | Version | Purpose |
|---|---|---|
| Kubernetes | 1.30.5 | Container orchestration |
| containerd | 1.7.22 | Container runtime |
| Calico | v3.28.2 | Pod networking (CNI) |
| Longhorn | 1.7.2 | Distributed block storage |

Default versions are validated and guaranteed to work together.
Custom versions can be set in the dashboard — incompatible combinations
are flagged with a warning before deployment.

---

## What you need

| Machine | Count | Min RAM | Min Disk | OS |
|---|---|---|---|---|
| Controller — runs the launcher | 1 | 1 GB | 10 GB | Ubuntu 22.04+ |
| Control plane | 1 | 2 GB | 20 GB | Ubuntu 22.04+ |
| Worker | 1 or more | 2 GB | 20 GB | Ubuntu 22.04+ |

All machines must reach each other over SSH.
You need the SSH password for each cluster machine — once, during bootstrap.

The controller is separate from the Kubernetes cluster intentionally.
If the cluster has issues, the launcher stays available to redeploy or reset.

---

## Start the launcher

```bash
# SSH into the controller VM
ssh user@<controller-ip>

# Clone
git clone https://github.com/sambett/k8s-launcher
cd k8s-launcher

# Install
pip3 install -r requirements.txt
export PATH=$PATH:~/.local/bin

# Start
python3 launcher.py
```

Open `http://<controller-ip>:5000` in your browser.

> If your browser cannot reach the controller directly, use an SSH tunnel:
> `ssh -L 5000:localhost:5000 user@<controller-ip>`
> then open `http://localhost:5000`

---

## One-time GitHub SSH setup (for contributors)

After a fresh clone, register the new SSH key on GitHub before pushing:

```bash
cat ~/.ssh/id_ed25519.pub
```

Go to https://github.com/settings/keys → New SSH key → paste → Save

Switch the remote to SSH:

```bash
git remote set-url origin git@github.com:sambett/k8s-launcher.git
```

---

## Dashboard tabs

### Bootstrap

| Step | Action | What happens |
|---|---|---|
| 1 | Install Ansible | Installs Ansible on the controller if absent |
| 2 | Bootstrap SSH | Connects to each node once with password, pushes SSH key permanently |
| 3 | Run preflight | Checks OS, RAM, disk, swap, Python 3 on every node |

Passwords are used once and never stored anywhere.

### Configure

- Fill in node IPs and hostnames
- Versions are pre-filled with the validated stack
- Incompatible version combinations show a warning instantly
- Click **Generate configuration** — creates Ansible inventory and variables
- Preview panels show exactly what will be used

Longhorn replicas are set automatically:

| Workers | Replicas |
|---|---|
| 1 | 1 |
| 2 | 2 |
| 3+ | 3 (max) |

### Deploy

Run phases in order. Each streams live Ansible output to the browser.

| Phase | Button | Duration |
|---|---|---|
| 4 | Deploy Kubernetes | 10–15 min |
| 5 | Validate Kubernetes | instant |
| 6 | Deploy Longhorn | 5–8 min |
| 7 | Validate Longhorn | instant |

Wait for each phase to show `__DONE__` before moving to the next.

### Status

- Full validation results for Kubernetes and Longhorn
- **Add worker node** — scale the cluster up without a terminal
- Join token display and copy
- Longhorn UI link
- kubeconfig download

### Reset

Two reset levels — both require typing `RESET` in the confirmation
field before anything runs.

**Reset cluster:**
Removes Kubernetes and Longhorn from all nodes. Packages uninstalled,
all Kubernetes directories removed, apt sources cleaned.
Nodes are left as clean Ubuntu — ready to redeploy immediately from
the Configure tab.

**Full wipe:**
Same as Reset cluster, plus removes the SSH authorized keys from all
cluster nodes and deletes the generated inventory and variables.
After a full wipe, you must re-run Bootstrap SSH to re-establish
passwordless access before redeploying.

Neither reset level touches Ansible on the controller, the SSH key on
the controller, or the launcher project files.

---

## After deployment

**Use kubectl locally:**
```bash
# download kubeconfig from Status tab, then:
mkdir -p ~/.kube
cp kubeconfig.yaml ~/.kube/config
kubectl get nodes
```

**Join token** — saved on the control plane at:
```
~/cluster-artifacts/join-command.txt
```
Expires after 24 hours. To regenerate:
```bash
kubeadm token create --print-join-command
```

**Longhorn UI** — URL shown in the Status tab after validation.

---

## Manual operation (advanced)

The launcher is a convenience layer. The Ansible projects are fully
self-contained and can be used directly without the dashboard.

Each project has its own inventory and variables:

```
ansible-k8s/
├── inventory/hosts.yml      ← node IPs, hostnames, SSH users
├── group_vars/all.yml       ← all cluster variables (versions, CIDRs, etc.)
└── site.yml                 ← run this to deploy

ansible-longhorn/
├── inventory/hosts.yml      ← same structure
├── group_vars/all.yml       ← Longhorn-specific variables
└── site.yml                 ← run this to deploy Longhorn
```

To deploy manually, edit these two files directly for each project
and run:

```bash
# deploy Kubernetes
cd ansible-k8s
ansible-playbook -i inventory/hosts.yml site.yml

# deploy Longhorn
cd ansible-longhorn
ansible-playbook -i inventory/hosts.yml site.yml
```

The launcher generates equivalent files at runtime in `generated/`
and passes them as `--extra-vars`. When using the projects manually,
edit the files inside the project folders directly — the launcher
will not interfere.

---

## Project layout

```
k8s-launcher/
├── launcher.py              entry point
├── requirements.txt         Python dependencies
├── compat_matrix.json       version compatibility rules
├── core/
│   ├── paths.py             path constants
│   └── ssh.py               SSH helpers
├── routes/
│   ├── bootstrap.py         install Ansible + push SSH keys
│   ├── preflight.py         node readiness checks
│   ├── configure.py         generate inventory + variables
│   ├── deploy.py            run playbooks + validate + add worker + reset
│   └── status.py            kubeconfig download
├── templates/
│   └── index.html           complete dashboard (single HTML file)
├── ansible-k8s/             Kubernetes playbooks (self-contained)
└── ansible-longhorn/        Longhorn playbooks (self-contained)
```

---

## Architecture

```
Browser → http://<controller-ip>:5000
                │
          controller VM
          (launcher + Ansible)
                │ SSH (passwordless after bootstrap)
      ┌─────────┼──────────┐
   cp node   worker1   worker2 ...
```

---

## Validated on

Ubuntu 22.04 LTS · Kubernetes 1.30.5 · Calico v3.28.2 · Longhorn 1.7.2
