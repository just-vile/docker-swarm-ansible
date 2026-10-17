# Minimal image for the Swarm autoscaler. Built locally on the manager by the
# 'autoscaler' Ansible role; never pushed to a registry (the service is pinned
# to the manager, so the image only needs to exist there).
FROM python:3.12-slim

RUN pip install --no-cache-dir \
    docker==7.1.0 \
    requests==2.32.3 \
    PyYAML==6.0.2

WORKDIR /app
COPY autoscaler.py /app/autoscaler.py

ENTRYPOINT ["python", "-u", "autoscaler.py"]
