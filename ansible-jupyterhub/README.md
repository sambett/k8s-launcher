# ansible-jupyterhub

Deploys JupyterHub on an existing Kubernetes cluster with GitLab OAuth authentication,
persistent user storage via Longhorn, and dynamic profile management.

**Prerequisites:** `ansible-k8s`, `ansible-longhorn`, and `ansible-gitlab` must all be
complete before running this playbook. `gitlab-outputs.json` must exist in `generated/`.

---

## Folder Structure
ansible-jupyterhub/
├── site.yml                          # Main playbook — three plays in sequence
├── group_vars/all.yml                # Variable reference (manual use only)
├── inventory/hosts.yml               # Inventory template (manual use only)
└── roles/
├── k8s_secrets/tasks/main.yml    # Creates namespace and all K8s secrets
├── jupyterhub/
│   ├── tasks/main.yml            # Helm deploy + readiness wait
│   └── templates/config.yaml.j2 # Full JupyterHub Helm values template
└── validate/tasks/main.yml       # Post-deploy sanity checks

---

## What the Playbook Does

The playbook runs three plays in order:

**Play 1 — Secrets** (`k8s_secrets` role)
Creates the `jhub` namespace and four Kubernetes secrets:
- `gitlab-oauth-secret` — GitLab OAuth client ID and secret
- `gitlab-registry-secret` — image pull credentials for the GitLab container registry
- `jupyterhub-crypt-key` — encrypts OAuth tokens stored in the JupyterHub database
- `jupyterhub-profiles` ConfigMap — initialized empty on first install only; never
  overwritten on redeploy so profiles created via the admin dashboard are preserved

**Play 2 — Deploy** (`jupyterhub` role)
Adds the JupyterHub Helm repo, renders `config.yaml` from the template, and runs
`helm upgrade --install`. Waits up to 10 minutes for the hub pod to reach Running.

**Play 3 — Validate** (`validate` role)
Confirms hub and proxy pods are Running, NodePort service exists, and prints the
access URL.

---

## Authentication Flow

Users authenticate via GitLab OAuth. On every login and spawn attempt, JupyterHub
calls the GitLab Groups API using the user's OAuth token to determine which profiles
they are allowed to see. Access is denied if the user belongs to no group that has
a profile defined.

Two dynamic hooks in `config.yaml.j2` implement this:
- `check_allowed` — gates login based on GitLab group membership
- `profile_list` — returns only profiles matching the user's groups at spawn time

---

## Profiles ConfigMap

The `jupyterhub-profiles` ConfigMap in the `jhub` namespace is the live profile
database. It is mounted inside the hub pod at `/etc/jupyterhub/profiles/profiles.json`.

It is managed exclusively by the workbench-admin dashboard after first install.
The playbook will never reset it on redeploy.

To inspect it directly:
```bash
kubectl get configmap jupyterhub-profiles -n jhub \
  -o jsonpath='{.data.profiles\.json}' | python3 -m json.tool
```

---

## Kubernetes Secrets Created

| Secret | Type | Purpose |
|---|---|---|
| `gitlab-oauth-secret` | Opaque | OAuth client ID + secret |
| `gitlab-registry-secret` | dockerconfigjson | Pull images from GitLab registry |
| `jupyterhub-crypt-key` | Opaque | Encrypts OAuth tokens at rest |

---

## Variables

In normal use all variables are generated automatically by the k8s-launcher into
`generated/jupyterhub-vars.yml`. The file `group_vars/all.yml` documents every
variable and serves as a reference for manual runs.

Key variables:

| Variable | What it controls |
|---|---|
| `jhub_chart_version` | JupyterHub Helm chart version (currently `3.3.8`) |
| `jhub_nodeport` | NodePort for JupyterHub UI (default `32080`) |
| `jhub_access_node_ip` | Worker IP used for the OAuth callback URL |
| `storage_class` | StorageClass for user PVCs (default `longhorn-jupyterhomes`) |
| `user_storage_capacity` | Size of each user's persistent volume (default `10Gi`) |

---

## Redeployment Safety

It is safe to rerun the playbook at any time. All secret tasks are idempotent.
The profiles ConfigMap is guarded — it will only be created if it does not already
exist. Helm will upgrade in place without affecting running user sessions or PVCs.
