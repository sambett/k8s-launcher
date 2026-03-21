# Accessing the Longhorn UI

Longhorn UI is already running as part of the Helm installation.
By default it is exposed as a `ClusterIP` service — reachable inside the cluster only.
Two options to access it from your browser:

---

## Option A — Port-Forward (recommended for lab, no cluster changes)

Run this on the control plane:

```bash
kubectl port-forward -n longhorn-system svc/longhorn-frontend 8080:80 --address 0.0.0.0
```

Then open your browser at:

```
http://10.110.188.76:8080
```

> `--address 0.0.0.0` makes the port available on all interfaces, not just localhost —
> required so you can reach it from your Windows machine.

To stop it: `Ctrl+C`

**Limitation:** the port-forward dies when the SSH session ends. Use Option B
if you want persistent access without keeping a terminal open.

---

## Option B — NodePort (persistent, survives SSH disconnect)

Patch the existing service to NodePort once:

```bash
kubectl patch svc longhorn-frontend -n longhorn-system \
  -p '{"spec":{"type":"NodePort","ports":[{"port":80,"targetPort":8000,"nodePort":30080}]}}'
```

Verify it took:

```bash
kubectl get svc longhorn-frontend -n longhorn-system
```

Expected output:

```
NAME                TYPE       CLUSTER-IP     PORT(S)        AGE
longhorn-frontend   NodePort   10.x.x.x       80:30080/TCP   Xm
```

Then open your browser at:

```
http://10.110.188.76:30080
```

This survives reboots and SSH disconnects. The UI is available as long as the
cluster is running.

### To undo and go back to ClusterIP

```bash
kubectl patch svc longhorn-frontend -n longhorn-system \
  -p '{"spec":{"type":"ClusterIP","ports":[{"port":80,"targetPort":8080}]}}'
```

---

## What You Will See

The Longhorn dashboard shows:

- **Node** tab — your worker node(s) with disk capacity and scheduling status
- **Volume** tab — all PVCs/volumes and their replica placement
- **StorageClass** — confirms `longhorn-jupyterhomes` is present
- **Setting** — all values from your `values.yaml` applied (replica count, data path, drain policy)

---

## Quick Checks to Run in the UI

| What to check | Where to look |
|---|---|
| Worker node is schedulable | Node tab → `ansibleworker` → Scheduling: Enabled |
| Disk is registered | Node tab → expand node → disk at `/var/lib/longhorn` |
| No degraded volumes | Volume tab → all volumes should show `Healthy` or be empty |
| Replica count matches config | Setting tab → Default Replica Count → should show `1` |
