# ─── Stage 0: Build CUE from source ─────────────────────────────────────────
# Build the CUE binary with a current Go toolchain so the embedded stdlib
# is up to date. Avoids shipping a pre-built binary whose Go version may lag.
FROM golang:1.26-alpine@sha256:91eda9776261207ea25fd06b5b7fed8d397dd2c0a283e77f2ab6e91bfa71079d AS cue-builder
ARG CUE_VERSION=v0.16.0
RUN go install cuelang.org/go/cmd/cue@${CUE_VERSION}

# ─── Stage 1: Python dependency builder ──────────────────────────────────────
# Install Python deps into a separate layer so the runtime image only copies
# the site-packages directory (not pip / build toolchain).
FROM python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97 AS builder

ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown

RUN apt-get update \
 && apt-get dist-upgrade -y \
 && apt-get install -y --no-install-recommends git curl build-essential \
 && rm -rf /var/lib/apt/lists/*

# uv — fast Python package installer; brought in via a pinned image copy
# so no pip install call is needed at build time.
COPY --from=ghcr.io/astral-sh/uv:0.6.17@sha256:4a6c9444b126bd325fba904bff796bf91fb777bf6148d60109c4cb1de2ffc497 \
     /uv /uv

WORKDIR /app

COPY requirements.txt .
RUN /uv pip install --system --no-cache --require-hashes -r requirements.txt

# Optional Kubernetes support for the ConfigMap and Helm sinks — off by default.
#   --build-arg KUBERNETES_CLIENT=kubernetes==31.0.0   Python client (ConfigMap sink)
#   --build-arg HELM_VERSION=v3.16.3                   helm CLI (Helm sink)
# The kubernetes client and its dependencies are not hash-pinned like
# requirements.txt; the helm archive is checked against its published SHA-256.
ARG KUBERNETES_CLIENT=""
ARG HELM_VERSION=""
ARG TARGETARCH=amd64
RUN mkdir -p /opt/extra-bin \
 && if [ -n "$KUBERNETES_CLIENT" ]; then /uv pip install --system --no-cache "$KUBERNETES_CLIENT"; fi \
 && if [ -n "$HELM_VERSION" ]; then \
      archive="helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" \
      && curl -fsSLo "/tmp/${archive}" "https://get.helm.sh/${archive}" \
      && echo "$(curl -fsSL "https://get.helm.sh/${archive}.sha256sum" | cut -d' ' -f1)  /tmp/${archive}" | sha256sum -c - \
      && tar -xzf "/tmp/${archive}" -C /tmp \
      && mv "/tmp/linux-${TARGETARCH}/helm" /opt/extra-bin/helm \
      && rm -rf "/tmp/${archive}" "/tmp/linux-${TARGETARCH}"; \
    fi

COPY . .

# ─── Stage 2: Runtime ────────────────────────────────────────────────────────
FROM python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97 AS runtime

ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="vartrack-orchestrator-service" \
      org.opencontainers.image.description="VarTrack GitOps ETL orchestrator — webhook receiver, CUE validator, multi-sink writer" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/your-org/vartrack" \
      org.opencontainers.image.licenses="Apache-2.0"

# Runtime system deps: git (for GitPython clone/fetch) only.
# Remove pip — the runtime never installs packages so pip is not needed here.
RUN apt-get update \
 && apt-get dist-upgrade -y \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && pip install "pip==26.1.1" \
 && pip uninstall -y pip \
 && rm -rf /usr/local/lib/python3.14/site-packages/pip*.dist-info

# CUE binary compiled from source.
COPY --from=cue-builder /go/bin/cue /usr/local/bin/cue

# Python packages from builder.
COPY --from=builder /usr/local/lib/python3.14/site-packages \
                    /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn
COPY --from=builder /usr/local/bin/celery  /usr/local/bin/celery
COPY --from=builder /usr/local/bin/gunicorn /usr/local/bin/gunicorn
# helm, when HELM_VERSION was set (empty otherwise).
COPY --from=builder /opt/extra-bin/ /usr/local/bin/

WORKDIR /app
COPY --from=builder /app /app

# Non-root user (uid 1001) — mirrors gateway-service :nonroot convention
# and Argo CD / Flux production container hardening.
# Create writable runtime dirs BEFORE chown so Docker volume mounts
# inherit the correct ownership when volumes are initialised from the image.
RUN useradd --uid 1001 --no-create-home --shell /bin/false vartrack \
 && mkdir -p /app/schema_registry /app/git_cache \
 && chown -R vartrack:vartrack /app
USER vartrack

# HTTP (uvicorn) + gRPC
EXPOSE 8000 50051

# Embed version metadata as environment variables for runtime inspection.
# PYTHONPATH includes generated proto stubs so "from vartrack.v1.services import webhook_pb2" resolves.
ENV APP_VERSION="${VERSION}" \
    GIT_COMMIT="${COMMIT}" \
    BUILD_DATE="${BUILD_DATE}" \
    PYTHONPATH="/app/app/generated/proto/python"

# ── Overrides for different container roles ───────────────────────────────────
# API + gRPC server (default):
#   docker run vartrack/orchestrator-service
#
# Celery webhook/sync worker:
#   command: ["celery", "-A", "app.worker.celery", "worker",
#             "--queues=webhooks,sync", "--loglevel=info",
#             "--concurrency=4", "--max-tasks-per-child=200"]
#
# Celery beat (periodic self-heal):
#   command: ["celery", "-A", "app.worker.celery", "beat", "--loglevel=info"]
#
# Flower (task monitoring):
#   command: ["celery", "-A", "app.worker.celery", "flower", "--port=5555"]
CMD ["python", "main.py"]
