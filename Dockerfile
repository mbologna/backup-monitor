FROM python:3.14.8-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS build

# git: apprise-notify is a git dependency. Installed into a virtualenv, so the
# runtime stage needs neither git nor pip.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE /tmp/build/
COPY src/ /tmp/build/src/
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir /tmp/build

FROM python:3.14.8-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 65534:65534
ENTRYPOINT ["backup-monitor"]
