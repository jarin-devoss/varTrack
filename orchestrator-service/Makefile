# ─── VarTrack Orchestrator-Service Makefile ──────────────────────────────────
#
# Usage:
#   make help           Show this help
#   make install        Install Python deps (editable + dev extras)
#   make rust           Build the Rust extension (vartrack_core)
#   make test           Run all tests (unit + integration)
#   make test-unit      Run unit tests only
#   make test-int       Run integration tests only
#   make lint           Ruff lint check
#   make fmt            Ruff format + fix
#   make typecheck      mypy type check
#   make check          lint + typecheck + test (full CI gate)
#   make dev            Start the API server locally (uvicorn --reload)
#   make worker         Start a Celery worker locally
#   make beat           Start Celery beat (periodic self-heal)
#   make flower         Start Flower task monitor
#   make docker-build   Build the Docker image
#   make docker-run     Run the Docker image locally
#   make clean          Remove build artefacts and caches

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ── Variables ─────────────────────────────────────────────────────────────────

IMAGE_NAME   ?= vartrack/orchestrator-service
IMAGE_TAG    ?= latest
VERSION      ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
COMMIT       ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
BUILD_DATE   ?= $(shell date -u +"%Y-%m-%dT%H:%M:%SZ")

PYTHON       := python
PIP          := pip
PYTEST       := pytest
RUFF         := ruff
MYPY         := mypy
CELERY_APP   := app.worker.celery

# ── Help ──────────────────────────────────────────────────────────────────────

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ── Install ───────────────────────────────────────────────────────────────────

.PHONY: install
install: ## Install Python dependencies (editable + dev extras)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

.PHONY: rust
rust: ## Build the Rust extension (vartrack_core) via maturin
	cd rust_core && maturin develop --release

.PHONY: rust-debug
rust-debug: ## Build the Rust extension in debug mode (faster compile)
	cd rust_core && maturin develop

# ── Testing ───────────────────────────────────────────────────────────────────

.PHONY: test
test: ## Run all tests (unit + integration)
	$(PYTEST) tests/ -v --tb=short

.PHONY: test-unit
test-unit: ## Run unit tests only
	$(PYTEST) tests/unit/ -v --tb=short

.PHONY: test-int
test-int: ## Run integration tests only
	$(PYTEST) tests/integration/ -v --tb=short

.PHONY: test-cov
test-cov: ## Run tests with coverage report
	$(PYTEST) tests/ -v --tb=short \
	  --cov=app --cov-report=term-missing --cov-report=html

# ── Linting & Formatting ──────────────────────────────────────────────────────

.PHONY: lint
lint: ## Ruff lint check (no fix)
	$(RUFF) check app/ tests/

.PHONY: fmt
fmt: ## Ruff format + auto-fix lint issues
	$(RUFF) format app/ tests/
	$(RUFF) check --fix app/ tests/

.PHONY: typecheck
typecheck: ## mypy static type check
	$(MYPY) app/ --ignore-missing-imports --no-error-summary

.PHONY: check
check: lint typecheck test ## Full CI gate: lint + typecheck + test

# ── Local Dev ─────────────────────────────────────────────────────────────────

.PHONY: dev
dev: ## Start the FastAPI + gRPC server with auto-reload
	uvicorn main:app --host 0.0.0.0 --port 8000 --reload

.PHONY: worker
worker: ## Start a Celery worker (webhooks + sync queues)
	celery -A $(CELERY_APP) worker \
	  --queues=webhooks,sync \
	  --loglevel=info \
	  --concurrency=4 \
	  --max-tasks-per-child=200

.PHONY: beat
beat: ## Start Celery beat for periodic self-heal
	celery -A $(CELERY_APP) beat --loglevel=info

.PHONY: flower
flower: ## Start Flower task monitoring UI (port 5555)
	celery -A $(CELERY_APP) flower --port=5555

# ── Docker ────────────────────────────────────────────────────────────────────

.PHONY: docker-build
docker-build: ## Build Docker image
	docker build \
	  --build-arg VERSION=$(VERSION) \
	  --build-arg COMMIT=$(COMMIT) \
	  --build-arg BUILD_DATE=$(BUILD_DATE) \
	  -t $(IMAGE_NAME):$(IMAGE_TAG) \
	  -f docker/Dockerfile .

.PHONY: docker-build-multi
docker-build-multi: ## Build multi-arch image (amd64 + arm64) and push
	docker buildx build \
	  --platform linux/amd64,linux/arm64 \
	  --build-arg VERSION=$(VERSION) \
	  --build-arg COMMIT=$(COMMIT) \
	  --build-arg BUILD_DATE=$(BUILD_DATE) \
	  -t $(IMAGE_NAME):$(IMAGE_TAG) \
	  -f docker/Dockerfile \
	  --push .

.PHONY: docker-run
docker-run: ## Run the Docker image locally (HTTP :8000, gRPC :50051)
	docker run --rm \
	  -p 8000:8000 \
	  -p 50051:50051 \
	  -e CELERY_BROKER_URL=redis://host.docker.internal:6379/0 \
	  -e CELERY_RESULT_BACKEND=redis://host.docker.internal:6379/1 \
	  $(IMAGE_NAME):$(IMAGE_TAG)

.PHONY: docker-worker
docker-worker: ## Run a Celery worker in Docker
	docker run --rm \
	  -e CELERY_BROKER_URL=redis://host.docker.internal:6379/0 \
	  -e CELERY_RESULT_BACKEND=redis://host.docker.internal:6379/1 \
	  $(IMAGE_NAME):$(IMAGE_TAG) \
	  celery -A $(CELERY_APP) worker --queues=webhooks,sync -l info

# ── Protobuf ──────────────────────────────────────────────────────────────────

.PHONY: proto
proto: ## Regenerate Python gRPC stubs from proto definitions
	cd .. && buf generate

# ── Cleanup ───────────────────────────────────────────────────────────────────

.PHONY: clean
clean: ## Remove build artefacts, caches, and generated files
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name ".coverage" -delete 2>/dev/null || true
	cd rust_core && cargo clean 2>/dev/null || true
