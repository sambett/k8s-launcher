# k8s-launcher

Deploy a production Kubernetes cluster from a browser.
No Ansible knowledge required. Works on any number of machines.

---

## What it deploys

| Component | Version |
|---|---|
| Kubernetes | 1.30.5 |
| containerd | 1.7.22 |
| Calico CNI | v3.28.2 |
| Longhorn storage | 1.7.2 |

---

## Requirements

| Machine | Count | Minimum spec |
|---|---|---|
| Controller — runs the launcher | 1 | Ubuntu 22.04+, Python 3.8+ |
| Control plane | 1 | Ubuntu 22.04+, 2 GB RAM, 20 GB disk |
| Workers | 1 or more | Ubuntu 22.04+, 2 GB RAM, 20 GB disk |

All machines must be reachable from the controller over SSH.
You need the SSH password for each machine — once, during bootstrap.

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

