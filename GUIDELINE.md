# Setup Guideline — Docker Swarm cluster with Ansible

A step-by-step guide to provisioning the 3-node Docker Swarm cluster (1 manager +
2 workers) from an Ubuntu WSL control node. Follow the sections in order the first
time; later you only need [Section 6](#6-deploy) to re-run.

> For a quick overview of the architecture and the monitoring stack, see
> [`README.md`](README.md). This guide is the detailed, do-this-then-that version.

---

## ⚠️ Read first: security

This repo currently contains a **private SSH key** (`id_ed25519`) and a **plaintext
Grafana password** (`grafana_admin_password: admin` in
[`group_vars/all.yml`](group_vars/all.yml)). That is acceptable only for a throwaway
lab. **Before pushing anywhere public or reusing this beyond the lab:**

- Delete `id_ed25519` from the repo and generate a fresh key (see
  [Section 4](#4-ssh-key-authentication)). Add `id_ed25519` to
  [`.gitignore`](.gitignore) so it is never committed again.
- Change the Grafana password and move secrets into
  [Ansible Vault](https://docs.ansible.com/ansible/latest/vault_guide/index.html).
- The **Traefik dashboard is served with `--api.insecure`**, i.e. no authentication, on
  port 8081. Same stance as the plaintext Grafana password: fine on an isolated lab LAN,
  not fine anywhere else. Put it behind a router with basic-auth middleware, or drop the
  `traefik_dashboard_port` publish, before this is reachable from a real network.

---

## 1. What you need

| Component       | Requirement                                                        |
|-----------------|--------------------------------------------------------------------|
| Control node    | Ubuntu on WSL2 (Windows 11), with network access to the VM LAN     |
| Ansible         | 2.14+ (`ansible --version`)                                        |
| Target VMs      | 3 × Ubuntu Server (22.04 / 24.04), reachable over SSH              |
| VM networking   | Static IPs on the same LAN as the table below                      |
| VM access       | A sudo-capable user (the inventory uses `root`) with SSH enabled   |

**Target layout** (defined in [`inventory/hosts.ini`](inventory/hosts.ini)):

| Role    | Hostname    | IP             |
|---------|-------------|----------------|
| Manager | swarm-mgr-1 | 192.168.71.100 |
| Worker  | swarm-wrk-1 | 192.168.71.101 |
| Worker  | swarm-wrk-2 | 192.168.71.102 |

If your IPs or usernames differ, edit [`inventory/hosts.ini`](inventory/hosts.ini)
before continuing (see [Section 5](#5-adjust-for-your-environment)).

---

## 2. Prepare the target VMs

On **each** of the three VMs:

1. Install a fresh Ubuntu Server and give it one of the static IPs above.
2. Make sure OpenSSH server is running and reachable:
   ```bash
   sudo systemctl enable --now ssh
   ```
3. Confirm the account you will connect as can run `sudo`. The inventory connects as
   `root`; if you use a different user, note it for [Section 5](#5-adjust-for-your-environment).
4. (Optional) Set the hostname to match the table so `docker node ls` reads cleanly:
   ```bash
   sudo hostnamectl set-hostname swarm-mgr-1   # or swarm-wrk-1 / swarm-wrk-2
   ```

No Docker install is needed — Ansible handles that.

---

## 3. Prepare the WSL control node

From your Ubuntu WSL shell:

```bash
# Install Ansible
sudo apt update && sudo apt install -y ansible

# Move into the project (adjust the path to where you cloned it)
cd /mnt/d/MSc/2026-2027_Sem1/Advance\ Networking/Project/docker-swarm-ansible

# Install the required Ansible collections (community.docker, community.general)
ansible-galaxy collection install -r requirements.yml
```

### WSL + Windows-drive gotcha (important)

This project lives on a Windows drive, which WSL mounts world-writable (`777`).
Ansible **ignores `ansible.cfg` in a world-writable directory** for security, so the
inventory and settings silently won't load. Point Ansible at the config explicitly:

```bash
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
```

Add that line to `~/.bashrc` to make it permanent, or — cleaner — copy the project
into your WSL home (`~/docker-swarm-ansible`), where permissions are normal and this
workaround isn't needed. Run **every** `ansible` / `ansible-playbook` command from the
project directory with `ANSIBLE_CONFIG` set.

---

## 4. SSH key authentication

Ansible logs into the VMs over SSH with no password prompt, so each VM must trust your
control node's public key.

> **Do not use the `id_ed25519` committed in this repo.** Generate your own:

```bash
# Generate a new key if you don't have one (press Enter for defaults)
ssh-keygen -t ed25519

# Copy the public key to each VM (use your VM username; the inventory uses root)
ssh-copy-id root@192.168.71.100
ssh-copy-id root@192.168.71.101
ssh-copy-id root@192.168.71.102
```

Test a direct login to confirm it's passwordless:

```bash
ssh root@192.168.71.100 'hostname'
```

> If root SSH is disabled on your VMs, either enable key-based root login, or connect
> as a normal sudo user and change `ansible_user` as described next.

---

## 5. Adjust for your environment

Edit these only if your setup differs from the defaults.

- **IPs / hostnames / SSH user** → [`inventory/hosts.ini`](inventory/hosts.ini).
  Change `ansible_user=root` under `[swarm:vars]` if you connect as a different user.
- **Monitoring, ports, image tags, Grafana password, firewall** →
  [`group_vars/all.yml`](group_vars/all.yml). Notable switches:
  - `monitoring_enabled: true` — set `false` to deploy the cluster without Prometheus/Grafana/Portainer.
  - `portainer_enabled: true` — set `false` to skip Portainer.
  - `configure_ufw: false` — set `true` to have the playbook open the required ports via ufw.
  - `grafana_admin_password: admin` — **change this.**
  - `traefik_enabled: true` — set `false` to deploy without the Traefik edge. The app then
    has no ingress unless you give the frontend its own published port back.
  - `traefik_http_port: 80` / `traefik_dashboard_port: 8081` — change if something else on
    your LAN already claims them.
  - `canary_sticky: false` — set `true` to pin each visitor to one version during a
    canary rollout (see [`../Todo-list/CANARY.md`](../Todo-list/CANARY.md)).

---

## 6. Deploy

### 6a. Verify connectivity first

```bash
ansible all -m ping          # expect "pong" from all 3 nodes
```

All three should return `SUCCESS` / `pong`. If any fails, see
[Troubleshooting](#8-troubleshooting) before deploying.

### 6b. Run the playbook

```bash
ansible-playbook site.yml
```

If `sudo` on the VMs needs a password, add `-K` (`--ask-become-pass`):

```bash
ansible-playbook site.yml -K
```

The run, in order (see [`site.yml`](site.yml)):

1. **common + docker** on all nodes — OS prep, Python Docker SDK, install Docker CE
   from the official APT repo, revert to the overlay2 image store (so cAdvisor can read
   container metrics).
2. **swarm** on the manager — `docker swarm init`, capture the worker join token.
3. **swarm** on the workers — join the swarm using that token.
4. **monitoring** on the manager — template configs and deploy the `monitoring` stack
   (skipped if `monitoring_enabled: false`).
5. **traefik** on the manager — create the `traefik-public` overlay, template the edge
   stack and the weighted canary router, install the canary helper scripts, deploy the
   `traefik` stack (skipped if `traefik_enabled: false`).
6. **autoscaler** on the manager — build the autoscaler image and deploy the
   `autoscaler` stack (skipped if `autoscaler_enabled: false`).
7. Print the list of swarm node hostnames.

---

## 7. Verify the cluster

```bash
# Nodes: manager should show Leader, all Ready / Active
ssh root@192.168.71.100 'docker node ls'

# Idempotency check: a second run should report changed=0
ansible-playbook site.yml
```

### Smoke-test a service

```bash
ssh root@192.168.71.100 \
  'docker service create --name web --replicas 3 -p 8080:80 nginx'
ssh root@192.168.71.100 'docker service ps web'   # tasks spread across nodes
curl http://192.168.71.100:8080                    # nginx welcome page

# teardown
ssh root@192.168.71.100 'docker service rm web'
```

### Monitoring UIs (manager IP)

| Service    | URL                                        | Notes                                              |
|------------|--------------------------------------------|----------------------------------------------------|
| Grafana    | <http://192.168.71.100:3000>               | login `admin` / password from `group_vars/all.yml` |
| Prometheus | <http://192.168.71.100:9090/targets>       | all 3 nodes `UP`, plus the `traefik` job           |
| Portainer  | <https://192.168.71.100:9443>              | set the admin password on first login              |
| Traefik    | <http://192.168.71.100:8081/dashboard/>    | routers / services the edge knows about            |

```bash
ssh root@192.168.71.100 'docker stack services monitoring'   # all replicas up
ssh root@192.168.71.100 'docker stack services traefik'      # 1/1
```

### Canary deployments

Traefik is here so a new app version can take a slice of live traffic instead of being
rolled out all at once. The runbook — build two versions, deploy the canary stack, step
the weights with `/opt/traefik/canary-weight.sh`, promote or roll back — lives in
[`../Todo-list/CANARY.md`](../Todo-list/CANARY.md).

---

## 8. Troubleshooting

| Symptom                                               | Likely cause / fix                                                                                                   |
|-------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------|
| `ansible all -m ping` → no hosts matched              | `ansible.cfg` not loaded. Set `export ANSIBLE_CONFIG="$PWD/ansible.cfg"` ([Section 3](#wsl--windows-drive-gotcha-important)). |
| `ping` → `UNREACHABLE` / permission denied            | SSH key not installed on the VM, or wrong `ansible_user`. Re-run `ssh-copy-id`; check the user in `hosts.ini`.      |
| `Missing sudo password`                               | Add `-K` to the `ansible-playbook` command.                                                                         |
| `couldn't resolve module community.docker`            | Collections not installed: `ansible-galaxy collection install -r requirements.yml`.                                 |
| Docker SDK / `jsondiff` errors on a node              | The `common` / `monitoring` roles install these via pip; re-run the playbook on that host.                          |
| Grafana "Swarm Containers" panels empty               | Containerd snapshotter still active. Ensure `docker_disable_containerd_snapshotter: true`, then re-run.             |
| Swarm nodes can't reach each other                    | Open ports 2377/tcp, 7946/tcp+udp, 4789/udp. On a flat LAN with ufw off they're already open; else set `configure_ufw: true`. |
| Workers join with the wrong address                   | Check `ansible_host` for each worker in `hosts.ini` and `swarm_advertise_addr` resolution in `group_vars/all.yml`.  |
| App stack fails with `network traefik-public not found` | The `traefik` play was skipped. Set `traefik_enabled: true` and re-run, or create the overlay by hand: `docker network create -d overlay --attachable traefik-public`. |
| Traefik task stuck `Preparing` / restarting            | It is manager-pinned and needs the Docker socket; check `docker service ps --no-trunc traefik_traefik`.             |
| Prometheus `traefik` job missing or DOWN               | Traefik must share the monitoring overlay. Confirm `monitoring_enabled: true` and re-run; see the canary troubleshooting table in [`../Todo-list/CANARY.md`](../Todo-list/CANARY.md). |

Useful flags while debugging:

```bash
ansible-playbook site.yml -vvv          # verbose output
ansible-playbook site.yml --check        # dry run (no changes)
ansible-playbook site.yml --limit swarm-wrk-1   # target one host
```

---

## 9. Tear down / reset

To leave the swarm and wipe it (run per node as needed):

```bash
# Stacks first, so nothing is still attached to the shared overlay.
ssh root@192.168.71.100 'docker stack rm todo-canary todo autoscaler monitoring traefik'
ssh root@192.168.71.100 'docker network rm traefik-public'
# Then dismantle the cluster: workers leave, manager last.
ssh root@192.168.71.101 'docker swarm leave'
ssh root@192.168.71.102 'docker swarm leave'
ssh root@192.168.71.100 'docker swarm leave --force'
```

> `docker stack rm` returns before the tasks are gone. If `docker network rm` reports the
> network is in use, wait a few seconds and retry.

Then re-run `ansible-playbook site.yml` to rebuild from scratch.

---

## Quick reference

```bash
# One-time
sudo apt install -y ansible
ansible-galaxy collection install -r requirements.yml
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
ssh-copy-id root@192.168.71.100   # + .101, .102

# Every run
ansible all -m ping
ansible-playbook site.yml         # add -K if sudo needs a password
ssh root@192.168.71.100 'docker node ls'
```
