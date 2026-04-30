# ─────────────────────────────────────────────────────────────────────────────
# Makefile — gateway-service
# ─────────────────────────────────────────────────────────────────────────────

APP_NAME    := gateway-service
DOCKER_IMG  := $(APP_NAME)
DOCKER_TAG  := latest
GO          := go
GOFLAGS     := -trimpath -ldflags="-s -w"

# ─── Development ─────────────────────────────────────────────────────────────

.PHONY: run
run: ## Run the gateway locally
	$(GO) run ./cmd/...

.PHONY: fmt
fmt: ## Format all Go source files
	$(GO) fmt ./...

.PHONY: vet
vet: ## Run go vet on all packages
	$(GO) vet ./...

# ─── Build ───────────────────────────────────────────────────────────────────

.PHONY: build
build: ## Build the binary
	CGO_ENABLED=0 $(GO) build $(GOFLAGS) -o bin/$(APP_NAME) ./cmd/...

# ─── Test ────────────────────────────────────────────────────────────────────

.PHONY: test
test: ## Run unit tests
	$(GO) test ./internal/... -count=1

.PHONY: test-verbose
test-verbose: ## Run unit tests with verbose output
	$(GO) test ./internal/... -count=1 -v

.PHONY: test-e2e
test-e2e: ## Run end-to-end tests
	$(GO) test ./e2e/... -count=1 -v -timeout 60s

.PHONY: test-all
test-all: test test-e2e ## Run all tests (unit + e2e)

.PHONY: test-cover
test-cover: ## Run tests with coverage report
	$(GO) test ./internal/... ./e2e/... -count=1 -coverprofile=coverage.out
	$(GO) tool cover -html=coverage.out -o coverage.html
	@echo "Coverage report → coverage.html"

.PHONY: test-race
test-race: ## Run tests with race detector
	$(GO) test ./internal/... ./e2e/... -count=1 -race

# ─── Lint ────────────────────────────────────────────────────────────────────

.PHONY: lint
lint: vet ## Lint all sources (go vet + staticcheck if available)
	@which staticcheck > /dev/null 2>&1 && staticcheck ./... || echo "staticcheck not installed, skipping"

# ─── Docker ──────────────────────────────────────────────────────────────────

.PHONY: docker-build
docker-build: ## Build Docker image
	docker build -t $(DOCKER_IMG):$(DOCKER_TAG) .

.PHONY: docker-run
docker-run: ## Run Docker container (requires config.cue in cwd)
	docker run --rm \
		-p 5657:5657 \
		-p 9090:9090 \
		-v $(CURDIR)/config.cue:/config.cue:ro \
		-e APP_ENV=test \
		-e LOG_LEVEL=INFO \
		-e ORCHESTRATOR_ADDR=host.docker.internal:50051 \
		-e GATEWAY_ADDR=:5657 \
		-e AGENT_ADDR=host.docker.internal:50052 \
		-e CONFIG_PATH=/config.cue \
		$(DOCKER_IMG):$(DOCKER_TAG)

# ─── Clean ───────────────────────────────────────────────────────────────────

.PHONY: clean
clean: ## Remove build artifacts and coverage files
	rm -rf bin/ coverage.out coverage.html

# ─── Help ────────────────────────────────────────────────────────────────────

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
