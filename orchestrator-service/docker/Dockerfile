# The build context is the repository root: the Python gRPC stubs are generated
# below from proto/ and the buf files, which live there.
#   docker build -f orchestrator-service/docker/Dockerfile .

# ─── Stage 0a: Python stubs from the protos ──────────────────────────────────
# orchestrator-service/app/generated is not in Git. The stubs are generated
# here with the same buf.gen.yaml CI and developers use, so an image never
# depends on what a checkout happens to hold. (buf generate also writes the Go
# code under libs/go/gen in this stage; it is discarded with the stage.)
FROM bufbuild/buf:1.75.0@sha256:adf82c7170be6629ec81dc907921ac1699407cd11c4f7fd7521b13c74152f849 AS proto
WORKDIR /src
COPY buf.yaml buf.lock buf.gen.yaml ./
COPY proto proto
RUN buf generate

# ─── Stage 0b: Build CUE from source ─────────────────────────────────────────
# Build the CUE binary with a current Go toolchain so the embedded stdlib
# is up to date. Avoids shipping a pre-built binary whose Go version may lag.
# It is built from tools/cue, a module of ours that names the CUE version and the
# versions of the libraries CUE depends on (golang.org/x/net, …): `go install …@vX`
# would build with the libraries CUE's own go.mod asks for, which only move when CUE
# makes a release. CI builds the same module, so it checks files with this binary.
FROM golang:1.27-alpine@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS cue-builder
WORKDIR /src/tools/cue
COPY tools/cue/go.mod tools/cue/go.sum ./
RUN go mod download
RUN CGO_ENABLED=0 go build -trimpath -o /out/cue cuelang.org/go/cmd/cue

# ─── Stage 0c: vartrack-bundle ───────────────────────────────────────────────
# The program that reads config.cue for the orchestrator exactly as the gateway and
# the watcher do (libs/go/bundle): one CUE evaluation, one set of shorthands and renamed
# options, one strict parse against the protos. The orchestrator is handed its output
# and does not read config.cue with rules of its own.
FROM golang:1.27-alpine@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS bundle-builder
WORKDIR /src/libs/go
COPY libs/go/go.mod libs/go/go.sum ./
RUN go mod download
COPY libs/go ./
RUN CGO_ENABLED=0 go build -trimpath -o /out/vartrack-bundle ./cmd/vartrack-bundle

# ─── Stage 1: Python dependency builder ──────────────────────────────────────
# The dependencies go into a virtual environment (/opt/venv) made by uv, which puts
# no pip in it, and the runtime image copies that directory and nothing else of this
# stage: no pip, no build toolchain, and none of pip's vendored libraries.
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS builder

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

COPY orchestrator-service/requirements.txt .
RUN /uv venv --python /usr/local/bin/python /opt/venv \
 && /uv pip install --python /opt/venv/bin/python --no-cache --require-hashes -r requirements.txt

# Optional Kubernetes support for the ConfigMap and Helm sinks — off by default.
#   --build-arg KUBERNETES_CLIENT=kubernetes==31.0.0   Python client (ConfigMap sink)
#   --build-arg HELM_VERSION=v3.16.3                   helm CLI (Helm sink)
# The kubernetes client and its dependencies are not hash-pinned like
# requirements.txt; the helm archive is checked against its published SHA-256.
ARG KUBERNETES_CLIENT=""
ARG HELM_VERSION=""
ARG TARGETARCH=amd64
RUN mkdir -p /opt/extra-bin \
 && if [ -n "$KUBERNETES_CLIENT" ]; then /uv pip install --python /opt/venv/bin/python --no-cache "$KUBERNETES_CLIENT"; fi \
 && if [ -n "$HELM_VERSION" ]; then \
      archive="helm-${HELM_VERSION}-linux-${TARGETARCH}.tar.gz" \
      && curl -fsSLo "/tmp/${archive}" "https://get.helm.sh/${archive}" \
      && echo "$(curl -fsSL "https://get.helm.sh/${archive}.sha256sum" | cut -d' ' -f1)  /tmp/${archive}" | sha256sum -c - \
      && tar -xzf "/tmp/${archive}" -C /tmp \
      && mv "/tmp/linux-${TARGETARCH}/helm" /opt/extra-bin/helm \
      && rm -rf "/tmp/${archive}" "/tmp/linux-${TARGETARCH}"; \
    fi

COPY orchestrator-service/ .
COPY --from=proto /src/orchestrator-service/app/generated app/generated

# ─── Stage 2: Runtime ────────────────────────────────────────────────────────
FROM python:3.14-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS runtime

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
# The base image's own pip is removed with its vendored libraries: the runtime never
# installs packages, and the dependencies below come from the builder's venv, which has no pip.
RUN apt-get update \
 && apt-get dist-upgrade -y \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y pip

# CUE binary compiled from source (stage 0b).
COPY --from=cue-builder /out/cue /usr/local/bin/cue

# Reads config.cue as the gateway does (see stage 0c).
COPY --from=bundle-builder /out/vartrack-bundle /usr/local/bin/vartrack-bundle

# Python packages from the builder: one venv, put first on the PATH so that `python`,
# `uvicorn`, `celery` and `gunicorn` are its own. It is at the same path as in the
# builder, and the base image is the same, so its interpreter link resolves.
COPY --from=builder /opt/venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"
# helm, when HELM_VERSION was set (empty otherwise).
COPY --from=builder /opt/extra-bin/ /usr/local/bin/

WORKDIR /app
COPY --from=builder /app /app

# Non-root user (uid 1001) — mirrors gateway-service :nonroot convention.
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
# Celery webhook/sync worker (syncs at once: config.cue's workers block, VARTRACK_WORKER_CONCURRENCY, --concurrency):
#   command: ["python", "-m", "app.worker"]
#
# Celery beat (periodic self-heal):
#   command: ["celery", "-A", "app.worker.celery", "beat", "--loglevel=info"]
#
# Flower (task monitoring) is not in the image: it is the `monitoring` extra of
# pyproject.toml, which this image does not install.
CMD ["python", "main.py"]
