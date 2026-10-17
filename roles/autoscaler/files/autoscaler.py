#!/usr/bin/env python3
"""Metric-driven autoscaler for Docker Swarm services.

Polls Prometheus for per-service CPU and memory usage and adjusts each
service's replica count via the Docker (manager) API. Inspired by
https://oneuptime.com/blog/post/2026-02-08-how-to-scale-docker-swarm-services-based-on-metrics/view
extended to react to memory as well as CPU.

Decision policy (per service, each cycle):
  * scale UP   if  CPU% >= cpu_high  OR   MEM% >= mem_high
  * scale DOWN if  CPU% <= cpu_low   AND  MEM% <= mem_low   (only available signals count)
  * otherwise hold
The target is clamped to [min, max], up-steps are larger than down-steps,
and a per-service cooldown prevents oscillation. Every decision is logged
with the metric values that triggered it.

Configuration comes from env (PROMETHEUS_URL, POLL_INTERVAL, RULES_FILE)
and a YAML rules file. The Docker socket is mounted read-write.
"""
import os
import sys
import time
import logging

import requests
import yaml
import docker

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://monitoring_prometheus:9090")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
RULES_FILE = os.environ.get("RULES_FILE", "/config/rules.yml")

# PromQL templates keyed by the Swarm service name (cAdvisor label).
# CPU is normalized to the container's CPU limit (quota/period), so the
# threshold means "percent of the service's CPU limit" and matches how memory
# is computed. Requires deploy.resources.limits.cpus on the target service.
CPU_EXPR = (
    'avg(rate(container_cpu_usage_seconds_total'
    '{{container_label_com_docker_swarm_service_name="{svc}"}}[2m])'
    ' / (container_spec_cpu_quota'
    '{{container_label_com_docker_swarm_service_name="{svc}"}}'
    ' / container_spec_cpu_period'
    '{{container_label_com_docker_swarm_service_name="{svc}"}})) * 100'
)
MEM_EXPR = (
    'avg(container_memory_usage_bytes'
    '{{container_label_com_docker_swarm_service_name="{svc}"}})'
    ' / avg(container_spec_memory_limit_bytes'
    '{{container_label_com_docker_swarm_service_name="{svc}"}}) * 100'
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("autoscaler")


def load_rules(path):
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("services", [])


def prom_query(expr):
    """Return the scalar result of an instant query, or None on no data/error."""
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": expr},
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("Prometheus query failed: %s", exc)
        return None

    if payload.get("status") != "success":
        return None
    result = payload.get("data", {}).get("result", [])
    if not result:
        return None
    value = float(result[0]["value"][1])
    # A service with no memory limit yields NaN/Inf for the MEM ratio; ignore it.
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def current_replicas(service):
    replicated = service.attrs["Spec"]["Mode"].get("Replicated")
    if not replicated:  # global service: cannot scale
        return None
    return int(replicated.get("Replicas", 0))


def decide(cpu, mem, current, rule):
    """Compute the clamped target replica count for one service."""
    scale_up = (cpu is not None and cpu >= rule["cpu_high"]) or (
        mem is not None and mem >= rule["mem_high"]
    )
    cpu_calm = cpu is None or cpu <= rule["cpu_low"]
    mem_calm = mem is None or mem <= rule["mem_low"]
    scale_down = (not scale_up) and cpu_calm and mem_calm

    if scale_up:
        target = current + rule["step_up"]
    elif scale_down:
        target = current - rule["step_down"]
    else:
        target = current
    return max(rule["min"], min(rule["max"], target))


def fmt(value):
    return "n/a" if value is None else f"{value:.1f}%"


def main():
    client = docker.from_env()
    rules = load_rules(RULES_FILE)
    log.info(
        "Autoscaler started: %d service(s), poll=%ds, prometheus=%s",
        len(rules), POLL_INTERVAL, PROMETHEUS_URL,
    )
    last_scaled = {}

    while True:
        for rule in rules:
            name = rule["name"]
            now = time.monotonic()

            cooldown_until = last_scaled.get(name, 0)
            if now < cooldown_until:
                continue

            try:
                service = client.services.get(name)
            except docker.errors.NotFound:
                log.warning("service %s not found, skipping", name)
                continue

            current = current_replicas(service)
            if current is None:
                log.warning("service %s is not replicated, skipping", name)
                continue

            cpu = prom_query(CPU_EXPR.format(svc=name))
            mem = prom_query(MEM_EXPR.format(svc=name))
            if cpu is None and mem is None:
                log.info("%s: no metrics yet, holding at %d", name, current)
                continue

            target = decide(cpu, mem, current, rule)
            if target == current:
                log.info(
                    "%s: hold at %d (cpu=%s mem=%s)",
                    name, current, fmt(cpu), fmt(mem),
                )
                continue

            direction = "UP" if target > current else "DOWN"
            log.info(
                "%s: scale %s %d -> %d (cpu=%s mem=%s)",
                name, direction, current, target, fmt(cpu), fmt(mem),
            )
            try:
                service.scale(target)
                last_scaled[name] = now + rule["cooldown"]
            except docker.errors.APIError as exc:
                log.error("failed to scale %s: %s", name, exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
