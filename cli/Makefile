MODULE  := github.com/vartrack/vtctl
BINARY  := vt
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")
COMMIT  ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo "unknown")

LDFLAGS := -ldflags "-X '$(MODULE)/internal/commands.Version=$(VERSION)' \
                     -X '$(MODULE)/internal/commands.GitCommit=$(COMMIT)' \
                     -s -w"

PLATFORMS := \
	linux/amd64   \
	linux/arm64   \
	darwin/amd64  \
	darwin/arm64  \
	windows/amd64

.PHONY: build install test lint clean release $(PLATFORMS)

## build: compile for the current platform
build:
	go build $(LDFLAGS) -o $(BINARY) ./cmd

## install: install into $GOPATH/bin (or $GOBIN)
install:
	go install $(LDFLAGS) ./cmd

## test: run all unit tests
test:
	go test ./... -count=1

## lint: run static analysis (requires golangci-lint)
lint:
	golangci-lint run ./...

## clean: remove build artefacts
clean:
	rm -rf $(BINARY) dist/

## release: cross-compile for all platforms into dist/
release: clean
	@mkdir -p dist
	@for platform in $(PLATFORMS); do \
		os=$$(echo $$platform | cut -d/ -f1); \
		arch=$$(echo $$platform | cut -d/ -f2); \
		out=dist/$(BINARY)-$$os-$$arch; \
		[ "$$os" = "windows" ] && out=$$out.exe; \
		echo "Building $$out ..."; \
		GOOS=$$os GOARCH=$$arch go build $(LDFLAGS) -o $$out ./cmd || exit 1; \
	done
	@echo "Artefacts written to dist/"

## help: print this help
help:
	@grep -E '^## ' Makefile | sed 's/^## /  /'
