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

The last play deploys a monitoring stack as a single Docker stack (`monitoring`)
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
