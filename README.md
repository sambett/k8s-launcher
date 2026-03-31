# k8s-launcher

Deploy a production Kubernetes cluster with distributed storage from your browser.
No Ansible, Kubernetes, or infrastructure knowledge required.

---

## What it deploys

| Component  | Version | Purpose                        |
|------------|---------|--------------------------------|
| Kubernetes | 1.30.5  | Container orchestration        |
| containerd | 1.7.22  | Container runtime              |
| Calico     | v3.28.2 | Pod networking (CNI)           |
| Longhorn   | 1.7.2   | Distributed block storage      |

Default versions are validated and guaranteed to work together.
Custom versions can be set in the dashboard — incompatible combinations
are flagged before deployment.

---

## What you need

| Machine        | Count    | Min RAM | Min Disk | OS           |
|----------------|----------|---------|----------|--------------|
| Controller     | 1        | 1 GB    | 10 GB    | Ubuntu 22.04+ |
| Control plane  | 1        | 2 GB    | 20 GB    | Ubuntu 22.04+ |
| Worker         | 1 or more| 2 GB    | 20 GB    | Ubuntu 22.04+ |

All machines must reach each other over SSH.
You need the SSH password for each cluster machine — once, during bootstrap.

The controller runs the launcher and is intentionally separate from the cluster.
If the cluster has issues, the launcher stays available to redeploy or reset.

---

## Start the launcher
```bash
ssh user@<controller-ip>

git clone https://github.com/sambett/k8s-launcher
cd k8s-launcher

pip3 install -r requirements.txt
export PATH=$PATH:~/.local/bin

python3 launcher.py
```

Open `http://<controller-ip>:5000` in your browser.

If your browser cannot reach the controller directly:
```bash
ssh -L 5000:localhost:5000 user@<controller-ip>
```
Then open `http://localhost:5000`.

---

## Dashboard tabs

### Bootstrap

| Step | Action          | What happens                                              |
|------|-----------------|-----------------------------------------------------------|
| 1    | Install Ansible | Installs Ansible on the controller if absent             |
| 2    | Bootstrap SSH   | Pushes SSH key to each node once — password never stored |
| 3    | Run preflight   | Checks OS, RAM, disk, swap, Python 3 on every node       |

### Configure

- Fill in node IPs and hostnames
- Versions pre-filled with validated defaults
- Incompatible combinations show a warning before deployment
- Click **Generate configuration** — creates Ansible inventory and variables

Longhorn replicas are set automatically:

| Workers | Replicas |
|---------|----------|
| 1       | 1        |
| 2       | 2        |
| 3+      | 3 (max)  |

### Kubernetes

Run phases in order. Each phase streams live Ansible output to the log panel.

| Phase | Button              | Duration   | What it does          |
|-------|---------------------|------------|-----------------------|
| 4     | Deploy Kubernetes   | 10–15 min  | Runs ansible-k8s      |
| 5     | Validate Kubernetes | instant    | 4 health checks       |
| 6     | Deploy Longhorn     | 5–8 min    | Runs ansible-longhorn |
| 7     | Validate Longhorn   | instant    | 4 storage checks      |

### Status

- Kubernetes and Longhorn validation results
- Join token display and copy
- Longhorn UI link
- Add worker node — scale the cluster without a terminal
- kubeconfig download

### Reset

Two levels — both require typing `RESET` before anything runs.

**Reset cluster:** Removes Kubernetes and Longhorn. Nodes left clean for redeploy.

**Full wipe:** Same as above plus removes SSH keys and generated inventory.
Use this to start completely from scratch.

---

## After deployment

### Use kubectl locally
```bash
# Download kubeconfig from Status tab, then:
mkdir -p ~/.kube
cp kubeconfig.yaml ~/.kube/config
kubectl get nodes
```

### Join token

Saved on the control plane at `~/cluster-artifacts/join-command.txt`.
Expires after 24 hours. To regenerate:
```bash
kubeadm token create --print-join-command
```

---

## Manual operation

Both Ansible projects work without the launcher. Edit two files, run one command:
```bash
# Kubernetes
cd ansible-k8s
# edit inventory/hosts.yml and group_vars/all.yml
ansible-playbook -i inventory/hosts.yml site.yml

# Longhorn
cd ansible-longhorn
# edit inventory/hosts.yml and group_vars/all.yml
ansible-playbook -i inventory/hosts.yml site.yml
```

---

## Project layout
```
k8s-launcher/
├── launcher.py              entry point (FastAPI, ~30 lines)
├── requirements.txt         Python dependencies
├── compat_matrix.json       version compatibility rules
├── core/
│   ├── paths.py             all path constants
│   ├── ssh.py               SSH helpers (paramiko)
│   └── ansible.py           shared Ansible stream + run_on_cp helpers
├── routes/
│   ├── bootstrap.py         Phases 0+1 — Ansible install + SSH key push
│   ├── preflight.py         Phase 2  — node readiness checks
│   ├── configure.py         Phase 3  — generate inventory + variables
│   ├── k8s.py               Phases 4+5 — Kubernetes deploy + validate
│   ├── longhorn.py          Phases 6+7 — Longhorn deploy + validate
│   ├── worker.py            Add worker node
│   ├── reset.py             Cluster reset + full wipe
│   └── status.py            kubeconfig download
├── templates/
│   └── index.html           complete dashboard (single HTML file)
├── ansible-k8s/             Kubernetes playbooks (self-contained)
├── ansible-longhorn/        Longhorn playbooks (self-contained)
└── workbench-admin/         Admin dashboard source code (deployed separately)
```

---

## Validated on

Ubuntu 22.04 LTS · Kubernetes 1.30.5 · Calico v3.28.2 · Longhorn 1.7.2
