# Docker Swarm cluster with Ansible

Provision a Docker Swarm cluster (1 manager + 2 workers) on Ubuntu VMs using Ansible.
Ansible runs from an **Ubuntu WSL** control node; the targets are VMware VMs on a local LAN.

| Role    | Hostname    | IP             |
|---------|-------------|----------------|
| Manager | swarm-mgr-1 | 192.168.71.100 |
| Worker  | swarm-wrk-1 | 192.168.71.101 |
| Worker  | swarm-wrk-2 | 192.168.71.102 |

## Layout

```text
ansible.cfg            # inventory path, ssh user, become, host-key policy
inventory/hosts.ini    # manager + worker groups
group_vars/all.yml     # docker + swarm variables
site.yml               # top-level playbook
roles/
  common/              # OS prep, Python Docker SDK, optional ufw rules
  docker/              # Docker CE install from the official APT repo
  swarm/               # swarm init on manager + join on workers
  monitoring/          # Prometheus + Grafana + Portainer stack (manager)
  traefik/             # Traefik edge + weighted canary router (manager)
  autoscaler/          # metric-driven replica autoscaler (manager)
requirements.yml       # community.docker, community.general collections
```

## 1. One-time setup (on the WSL control node)

```bash
# Install Ansible
sudo apt update && sudo apt install -y ansible

# Install required collections
ansible-galaxy collection install -r requirements.yml

# SSH key auth to each VM (replace 'ubuntu' if your sudo user differs)
ssh-keygen -t ed25519            # if you don't already have a key
ssh-copy-id root@192.168.71.100
ssh-copy-id root@192.168.71.101
ssh-copy-id root@192.168.71.102
```

If your VMs use a different sudo username, change `ansible_user` in
[`inventory/hosts.ini`](inventory/hosts.ini). If sudo requires a password, add
`--ask-become-pass` (`-K`) to the `ansible-playbook` command below.

### WSL note (important)

This project sits on a Windows drive, which WSL mounts world-writable (`/mnt/d/...`,
permissions `777`). Ansible **ignores `ansible.cfg` in a world-writable directory** for
security, so the inventory and settings won't load unless you point to the config explicitly:

```bash
cd /mnt/d/MSc/2026-2027_Sem1/Advance\ Networking/Project/docker-swarm-ansible
export ANSIBLE_CONFIG="$PWD/ansible.cfg"   # add to ~/.bashrc to make it permanent
```

`ANSIBLE_CONFIG` is honored regardless of directory permissions. (Alternative: copy the
project into your WSL home, e.g. `~/docker-swarm-ansible`, where permissions are normal.)
Run every `ansible`/`ansible-playbook` command below from this directory with that variable set.

## 2. Verify connectivity

```bash
cd docker-swarm-ansible
ansible all -m ping          # expect "pong" from all 3 nodes
```

## 3. Deploy the cluster

```bash
ansible-playbook site.yml
# add -K if sudo needs a password
```

The run installs Docker on all nodes, initializes the swarm on the manager, joins both
workers, and prints the list of swarm nodes at the end.

## 4. Verify the cluster

```bash
ssh ubuntu@192.168.71.100 'docker node ls'
```

Expect 3 nodes: the manager showing `Leader`, all `Ready` / `Active`.

Re-run `ansible-playbook site.yml` — it should report `changed=0` (idempotent).

### Smoke test a service

```bash
ssh ubuntu@192.168.71.100 \
  'docker service create --name web --replicas 3 -p 8080:80 nginx'
ssh ubuntu@192.168.71.100 'docker service ps web'   # tasks spread across nodes
curl http://192.168.71.100:8080                      # nginx welcome page

# teardown
ssh ubuntu@192.168.71.100 'docker service rm web'
```

## 5. Monitoring stack

A dedicated play deploys a monitoring stack as a single Docker stack (`monitoring`)
onto the swarm. It is controlled from [`group_vars/all.yml`](group_vars/all.yml)
(`monitoring_enabled`, ports, image tags, `portainer_enabled`).

| Service        | Mode             | What it does                              |
|----------------|------------------|-------------------------------------------|
| node-exporter  | global (1/node)  | host metrics (CPU, RAM, disk, net)        |
| cAdvisor       | global (1/node)  | per-container metrics                     |
| Prometheus     | manager-pinned   | scrape + TSDB, Swarm service discovery    |
| Grafana        | manager-pinned   | dashboards (datasource + 2 pre-loaded)    |
| Portainer      | manager + agents | cluster / stack management UI             |

Prometheus auto-discovers the exporters on every node via Docker Swarm service
discovery (`dockerswarm_sd_config`), so adding or removing a worker needs no
config change. Prometheus and Grafana keep their data in named volumes pinned
to the manager.

Deploy happens as part of `ansible-playbook site.yml`. After it finishes:

```bash
ssh root@192.168.71.100 'docker stack services monitoring'   # all replicas up
```

Open in a browser (manager IP):

- Grafana — <http://192.168.71.100:3000> (login `admin` / the password in
  `group_vars/all.yml`); the Prometheus datasource and the **Swarm Nodes** /
  **Swarm Containers** dashboards are pre-provisioned.
- Prometheus — <http://192.168.71.100:9090/targets> (all 3 nodes `UP`).
- Portainer — <https://192.168.71.100:9443> (set the admin password on first
  login; the cluster view shows all 3 nodes via the agents).

To deploy the cluster without monitoring, set `monitoring_enabled: false`.
Drop Portainer with `portainer_enabled: false`.

## 6. Traefik edge & canary deployments

The `traefik` role deploys a manager-pinned Traefik (stack `traefik`) that becomes the
cluster's **only HTTP ingress** on port 80, replacing the frontend's old `8080:3000`
publish. Its reason for existing is traffic splitting: Traefik can route a chosen
percentage of live requests to a second version of a service, which the swarm ingress
mesh cannot do.

| Setting | Where | Meaning |
|---------|-------|---------|
| `traefik_enabled` | `group_vars` | master switch for the play |
| `traefik_http_port` / `traefik_dashboard_port` | `group_vars` | published ports (80 / 8081) |
| `traefik_public_network` | `group_vars` | shared edge overlay, created by the role |
| `canary_initial_*_weight` | `group_vars` | the split the playbook writes on every run |
| `canary_sticky` | `group_vars` | pin a visitor to one version with a cookie (off by default) |

Two providers, two jobs: the **Swarm provider** discovers each frontend service from the
`deploy.labels` in `Todo-list/docker-stack.yml` and `docker-stack-canary.yml`, while the
**file provider** (`/opt/traefik/dynamic/canary.yml`) owns the weighted router — a
weighted service cannot be declared in labels at all. Traefik watches that file, so
shifting traffic is a write, not a restart.

The role also installs two helpers on the manager:

```bash
/opt/traefik/canary-weight.sh 90 10   # 10% of requests to the canary
/opt/traefik/canary-weight.sh 100 0   # all traffic back to stable (rollback)
/opt/traefik/split-check.sh 200       # measure the split that is actually happening
```

Traefik's own Prometheus metrics are scraped through the same `dockerswarm_sd` pattern as
the exporters, and Grafana gets a **Canary Rollout (Traefik)** dashboard: observed share,
per-version error ratio and per-version latency.

- Traefik dashboard — <http://192.168.71.100:8081/dashboard/>
- App (through Traefik) — <http://192.168.71.100/>

The full runbook — build two versions, deploy the canary, step the weights, promote or
roll back — is in **[`../Todo-list/CANARY.md`](../Todo-list/CANARY.md)**.

To deploy the cluster without Traefik, set `traefik_enabled: false` — but then revert the
frontend in `docker-stack.yml` to publishing its own port, or the app has no ingress.

## 7. Autoscaler

The final play deploys a **metric-driven autoscaler** (role `autoscaler`, stack
`autoscaler`) onto the manager. It polls the monitoring stack's Prometheus for
each target service's CPU and memory and changes replica counts via the Docker
API — no extra metrics infrastructure, it reuses Prometheus + cAdvisor.

| Setting                   | Where            | Meaning                                  |
|---------------------------|------------------|------------------------------------------|
| `autoscaler_enabled`      | `group_vars`     | master switch for the play               |
| `autoscaler_poll_interval`| `group_vars`     | seconds between evaluations (default 30) |
| `autoscale_services`      | `group_vars`     | per-service thresholds / bounds / steps  |

Decision policy per service, each cycle: scale **up** if `CPU% ≥ cpu_high`
**or** `MEM% ≥ mem_high`; scale **down** only if `CPU% ≤ cpu_low` **and**
`MEM% ≤ mem_low`; the target is clamped to `[min, max]`, up-steps are larger
than down-steps, and a per-service `cooldown` prevents oscillation. Both CPU
and memory are measured as a **percentage of the service's own limit**, so the
thresholds are limit-relative (e.g. `cpu_high: 80` = 80% of `limits.cpus`).

The autoscaler builds its own small Python image **locally on the manager**
(Swarm doesn't build; the service is manager-pinned so the image only needs to
exist there) and joins the existing `monitoring_monitoring` overlay network to
reach Prometheus. It scales the **todo** app's `todo_frontend` only;
`todo_backend` and `todo_db` (single-replica MongoDB, stateful) are left out.

**Prerequisites on the target services** (already applied in the todo
`docker-stack.yml`): each scalable service must declare
`deploy.resources.limits.memory` so memory can be read as a percentage of its
limit, and its compose `replicas` should equal the autoscaler's `min`.

> Caveat: re-running `docker stack deploy -c docker-stack.yml todo` resets
> replicas to the compose value, overriding the autoscaler until the next
> cycle. Keeping compose `replicas` == `min` makes that reset harmless.

The autoscaler and the canary router stay out of each other's way on purpose: the canary
runs as its own stack (`todo-canary_frontend`), which matches no rule here, and the
traffic split is set by Traefik's weights rather than by replica counts — so scaling and
exposure remain independent decisions. Expect stable to scale *down* during a rollout as
traffic moves off it.

Watch it work:

```bash
ssh root@192.168.71.100 'docker service logs -f autoscaler_autoscaler'
# in another shell, drive load at the todo frontend (through Traefik on :80):
hey -z 120s -c 100 http://192.168.71.100/
ssh root@192.168.71.100 'docker service ls'   # todo_frontend climbs, then falls after load stops
```

To deploy the cluster without the autoscaler, set `autoscaler_enabled: false`.

## Notes

- **Swarm ports**: 2377/tcp (management), 7946/tcp+udp (discovery), 4789/udp (overlay).
  On a flat lab LAN with `ufw` disabled these are already open. To manage them with ufw,
  set `configure_ufw: true` in [`group_vars/all.yml`](group_vars/all.yml).
- **Docker repo**: installs current Docker CE from `download.docker.com`, not Ubuntu's older
  `docker.io` package.
- **Secrets**: the worker join token is passed in memory between plays and never written to
  the repo. If you later add registry credentials, use Ansible Vault.

## Next steps (optional)

- Deploy an app with `docker stack deploy -c stack.yml <name>` and an overlay network.
- Add more managers for a highly-available control plane (use an odd number: 3 or 5).
