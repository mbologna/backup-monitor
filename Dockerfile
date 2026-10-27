FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.14.8-slim@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a AS build

# git: apprise-notify is a git dependency. Installed into a virtualenv, so the
# runtime stage needs neither git nor uv.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
# Install exactly what uv.lock pins, checking its hashes, rather than
# re-resolving pyproject.toml at build time. Bytecode is compiled here because
# the runtime image sets PYTHONDONTWRITEBYTECODE and runs read-only.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON=/usr/local/bin/python3 UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1
WORKDIR /tmp/build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14.8-slim@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 65534:65534
ENTRYPOINT ["backup-monitor"]
