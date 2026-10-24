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

Configuration comes from env (PROMETHEUS_URL, POLL_INTERVAL, RATE_WINDOW,
RULES_FILE) and a YAML rules file. The Docker socket is mounted read-write.
"""
import os
import sys
import time
import logging

import requests
import yaml
import docker

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://monitoring_prometheus:9090")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "15"))
RULES_FILE = os.environ.get("RULES_FILE", "/config/rules.yml")
# Averaging window for the CPU rate. This is the autoscaler's reaction time:
# rate() reports the *average* over the window, so a burst shorter than the
# window is scaled down by (burst / window) and can stay under cpu_high for
# its entire duration. Keep it >= 4 Prometheus scrape intervals.
RATE_WINDOW = os.environ.get("RATE_WINDOW", "60s")

# PromQL templates keyed by the Swarm service name (cAdvisor label).
# CPU is normalized to the container's CPU limit (quota/period), so the
# threshold means "percent of the service's CPU limit" and matches how memory
# is computed. Requires deploy.resources.limits.cpus on the target service.
#
# Both ratios aggregate each side to a single label-less value *before*
# dividing. That is not cosmetic: PromQL's one-to-one vector matching only
# pairs samples whose label sets are identical, and cAdvisor tags
# container_cpu_usage_seconds_total with cpu="total" while the
# container_spec_* gauges carry no cpu label. Dividing the raw selectors
# therefore matches nothing and the query silently returns no data — which
# this autoscaler reports as "cpu=n/a" and treats as a calm signal forever.
# sum()/sum() is also the figure we actually want: total cores used over
# total cores granted = mean utilisation across the service's replicas.
CPU_EXPR = (
    'sum(rate(container_cpu_usage_seconds_total'
    '{{container_label_com_docker_swarm_service_name="{svc}"}}[{window}]))'
    ' / sum(container_spec_cpu_quota'
    '{{container_label_com_docker_swarm_service_name="{svc}"}}'
    ' / container_spec_cpu_period'
    '{{container_label_com_docker_swarm_service_name="{svc}"}}) * 100'
)
MEM_EXPR = (
    'sum(container_memory_usage_bytes'
    '{{container_label_com_docker_swarm_service_name="{svc}"}})'
    ' / sum(container_spec_memory_limit_bytes'
    '{{container_label_com_docker_swarm_service_name="{svc}"}}) * 100'
)
# Diagnostics, run only when CPU_EXPR comes back empty: the two halves of the
# ratio fail for unrelated reasons and need opposite fixes, so the warning
# asks Prometheus which one is missing instead of guessing.
CPU_LIMIT_EXPR = (
    'sum(container_spec_cpu_quota'
    '{{container_label_com_docker_swarm_service_name="{svc}"}}'
    ' / container_spec_cpu_period'
    '{{container_label_com_docker_swarm_service_name="{svc}"}})'
)
CPU_SAMPLES_EXPR = (
    'max(count_over_time(container_cpu_usage_seconds_total'
    '{{container_label_com_docker_swarm_service_name="{svc}"}}[{window}]))'
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


def task_health(service):
    """Return (running task count, newest task error) for a service.

    Used to refuse scaling up a service that cannot even place the replicas
    it already has — an unpullable image, for instance, makes every new task
    Rejected, so adding replicas just multiplies the rejections silently.
    """
    try:
        tasks = service.tasks()
    except (docker.errors.DockerException, requests.RequestException) as exc:
        log.warning("could not list tasks for %s: %s", service.name, exc)
        return None, None

    running = 0
    newest_ts = ""
    last_error = None
    for task in tasks:
        status = task.get("Status", {})
        state = status.get("State")
        if state == "running":
            running += 1
            continue
        err = status.get("Err")
        # Task timestamps are RFC3339, so string order is chronological.
        ts = task.get("UpdatedAt", "")
        if err and state in ("rejected", "failed") and ts >= newest_ts:
            newest_ts, last_error = ts, err
    return running, last_error


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


def cpu_blind_hint(name):
    """Say which half of the CPU ratio is missing, for the cpu=n/a warning.

    No quota means the service declares no deploy.resources.limits.cpus, so
    cAdvisor omits container_spec_cpu_quota and the denominator is empty.
    A quota plus fewer than two samples in the window means the numerator is
    empty instead: rate() needs two points and returns nothing with one, so
    RATE_WINDOW is too short for the Prometheus scrape interval. Memory does
    not show this because an instant selector gets a 5m lookback.
    """
    # 0 counts as absent: a container with no quota reports 0, not a limit.
    if not prom_query(CPU_LIMIT_EXPR.format(svc=name)):
        return ("no container_spec_cpu_quota - the service declares no "
                "deploy.resources.limits.cpus")
    samples = prom_query(CPU_SAMPLES_EXPR.format(svc=name, window=RATE_WINDOW))
    if samples is not None and samples < 2:
        return (f"only {samples:.0f} scrape(s) in the {RATE_WINDOW} rate "
                "window, rate() needs 2 - raise RATE_WINDOW to >= 4 scrape "
                "intervals")
    return "the CPU query returned no data"


def main():
    client = docker.from_env()
    rules = load_rules(RULES_FILE)
    log.info(
        "Autoscaler started: %d service(s), poll=%ds, window=%s, prometheus=%s",
        len(rules), POLL_INTERVAL, RATE_WINDOW, PROMETHEUS_URL,
    )
    last_scaled = {}
    # Consecutive failed lookups per service, so a service that stays absent
    # (or a redeploy that briefly removes it) does not fill the log with one
    # identical warning per poll.
    lookup_misses = {}
    # Consecutive polls with no CPU reading per service. A set would not do:
    # the signal flaps (one sample lands in the rate window, the next does
    # not), and clearing on a single success re-warns every other cycle.
    cpu_misses = {}

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
                misses = lookup_misses.get(name, 0) + 1
                lookup_misses[name] = misses
                # First miss, then every 10th: a stack redeploy makes the
                # service vanish for a cycle or two and that is not news.
                if misses == 1 or misses % 10 == 0:
                    log.warning(
                        "service %s not found, skipping (%d consecutive)",
                        name, misses,
                    )
                continue
            except (docker.errors.DockerException, requests.RequestException) as exc:
                # A blip on the manager socket must not kill the loop: without
                # this the exception escapes, the container exits, and the
                # service only scales again once Swarm has restarted it.
                log.warning("docker API error looking up %s: %s", name, exc)
                continue

            if lookup_misses.pop(name, 0):
                log.info("service %s found again", name)

            current = current_replicas(service)
            if current is None:
                log.warning("service %s is not replicated, skipping", name)
                continue

            cpu = prom_query(CPU_EXPR.format(svc=name, window=RATE_WINDOW))
            mem = prom_query(MEM_EXPR.format(svc=name))
            if cpu is None and mem is None:
                log.info("%s: no metrics yet, holding at %d", name, current)
                continue

            if cpu is None:
                # Memory is reporting, so cAdvisor is being scraped and the
                # containers exist: something in CPU_EXPR alone is empty.
                misses = cpu_misses.get(name, 0) + 1
                cpu_misses[name] = misses
                # First miss, then every 10th, as for a missing service: the
                # extra queries behind the hint are not worth one per poll.
                if misses == 1 or misses % 10 == 0:
                    log.warning(
                        "%s: no CPU signal (memory is reporting) - %s "
                        "(%d consecutive)",
                        name, cpu_blind_hint(name), misses,
                    )
            elif cpu_misses.pop(name, 0):
                log.info("%s: CPU signal back", name)

            target = decide(cpu, mem, current, rule)
            if target == current:
                log.info(
                    "%s: hold at %d (cpu=%s mem=%s)",
                    name, current, fmt(cpu), fmt(mem),
                )
                continue

            if target > current:
                running, task_error = task_health(service)
                if running is not None and running < current:
                    log.warning(
                        "%s: not scaling up to %d, only %d/%d task(s) running%s",
                        name, target, running, current,
                        f" - last task error: {task_error}" if task_error else "",
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
            except (docker.errors.DockerException, requests.RequestException) as exc:
                log.error("failed to scale %s: %s", name, exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
