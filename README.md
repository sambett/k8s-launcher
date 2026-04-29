# k8s-launcher

Browser-based deployment tool for provisioning a production-grade Kubernetes cluster
on bare Ubuntu VMs — no Ansible or Kubernetes knowledge required.

---

## 1. Launching the Platform

### Install dependencies

```bash
cd ~/k8s-launcher
pip3 install -r requirements.txt
```

### Start

```bash
cd ~/k8s-launcher
python3 launcher.py
```

Open your browser at: **http://\<controller-ip\>:5000**

### Redeploy / restart

```bash
pkill -f launcher.py
cd ~/k8s-launcher
python3 launcher.py
```

---

## 2. Adding a GPU Worker Node

Run these commands **on the GPU node itself** before registering it in the launcher.
Each block checks first — if the requirement is already met it skips the install.

### Step 1 — Verify NVIDIA driver

```bash
nvidia-smi || echo "DRIVER MISSING — install before continuing"
```

If missing, install the driver and reboot:

```bash
sudo apt update && sudo apt install -y nvidia-driver-525
sudo reboot
# After reboot, verify:
nvidia-smi
```

### Step 2 — Verify NVIDIA Container Toolkit

```bash
nvidia-ctk --version 2>/dev/null || (
  echo "Toolkit not found — installing..." &&
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg &&
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list &&
  sudo apt update && sudo apt install -y nvidia-container-toolkit
)
```

Verify the installed version meets the minimum (≥ 1.7.0):

```bash
nvidia-ctk --version
```

### Step 3 — Configure containerd NVIDIA runtime

```bash
grep -rl nvidia-container-runtime /etc/containerd/ 2>/dev/null | grep -q . \
  && echo "containerd already configured" \
  || (
    echo "Configuring containerd NVIDIA runtime..." &&
    sudo nvidia-ctk runtime configure --runtime=containerd --set-as-default &&
    sudo systemctl restart containerd &&
    echo "Done"
  )
```

### Step 4 — Verify the runtime is loaded in the running daemon

```bash
containerd config dump 2>/dev/null | grep -c nvidia-container-runtime \
  | grep -q "^0$" \
  && echo "WARNING: runtime not loaded — restart containerd: sudo systemctl restart containerd" \
  || echo "OK: NVIDIA runtime is active in containerd"
```

### Step 5 — Final check (all 4 at once)

```bash
echo "=== 1. Driver ===" && nvidia-smi --query-gpu=driver_version,name --format=csv,noheader
echo "=== 2. Toolkit ===" && nvidia-ctk --version
echo "=== 3. Config file ===" && grep -rl nvidia-container-runtime /etc/containerd/
echo "=== 4. Runtime loaded ===" && containerd config dump 2>/dev/null | grep -c nvidia-container-runtime
```

All four must return real output. Step 4 must return a number **greater than 0**.

---

Once all four pass, go to the launcher → **Workers tab** → add the node → then
**Extensions tab** → select the node → **Check**. All four should be green immediately.

