# k8s-launcher

A browser-based platform for deploying and managing an on-prem Kubernetes AI Workbench.
It provisions the cluster, installs storage, deploys GitLab and JupyterHub, and exposes an admin dashboard for managing profiles, users, images, and GPU policies.

---

## 1. Launching the Platform

### Prerequisites

    sudo apt update && sudo apt install -y python3-pip git
    cd ~/k8s-launcher
    python3 -m pip install --user --break-system-packages -r requirements.txt

requirements.txt installs: fastapi, uvicorn, paramiko, jinja2, pyyaml, ansible.

Add pip binaries to PATH permanently:

    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc

Verify ansible is reachable:

    ansible --version

### Start

    cd ~/k8s-launcher
    python3 launcher.py

Open your browser at: http://<controller-ip>:5000

### Restart

    pkill -f launcher.py
    cd ~/k8s-launcher
    python3 launcher.py

---

## 2. Adding a GPU Worker Node

Run these on the GPU node before registering it in the launcher.

### Step 1 — Verify NVIDIA driver

    nvidia-smi || echo "DRIVER MISSING — install before continuing"

If missing:

    sudo apt update && sudo apt install -y nvidia-driver-525
    sudo reboot
    nvidia-smi

### Step 2 — Verify NVIDIA Container Toolkit

    nvidia-ctk --version 2>/dev/null || (
      echo "Toolkit not found — installing..." &&
      curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg &&
      curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list &&
      sudo apt update && sudo apt install -y nvidia-container-toolkit
    )

Verify version is >= 1.7.0:

    nvidia-ctk --version

### Step 3 — Configure containerd NVIDIA runtime

    grep -rl nvidia-container-runtime /etc/containerd/ 2>/dev/null | grep -q . \
      && echo "containerd already configured" \
      || (
        sudo nvidia-ctk runtime configure --runtime=containerd --set-as-default &&
        sudo systemctl restart containerd &&
        echo "Done"
      )

### Step 4 — Verify runtime is active

    sudo containerd config dump 2>/dev/null | grep -c nvidia-container-runtime \
      | grep -q "^0$" \
      && echo "WARNING: runtime not loaded — run: sudo systemctl restart containerd" \
      || echo "OK: NVIDIA runtime is active in containerd"

### Step 5 — Final check

    echo "=== 1. Driver ===" && nvidia-smi --query-gpu=driver_version,name --format=csv,noheader
    echo "=== 2. Toolkit ===" && nvidia-ctk --version
    echo "=== 3. Config file ===" && grep -rl nvidia-container-runtime /etc/containerd/
    echo "=== 4. Runtime loaded ===" && sudo containerd config dump 2>/dev/null | grep -c nvidia-container-runtime

All four must return real output. Step 4 must return a number greater than 0.

Once all four pass: launcher -> Workers tab -> add the node -> Extensions tab -> select the node -> Check -> all green.
