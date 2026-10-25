# ── Build stage ────────────────────────────────────────────────────────────────
FROM golang:1.27-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS builder

# The build context is the repository root: the watcher builds against the
# shared module in libs/go (its go.mod replaces vartrack/libs with ../libs/go).
WORKDIR /build

COPY libs/go/go.mod libs/go/go.sum libs/go/
COPY watcher-service/go.mod watcher-service/go.sum watcher-service/
WORKDIR /build/watcher-service
RUN go mod download

WORKDIR /build
COPY libs/go libs/go
COPY watcher-service watcher-service
WORKDIR /build/watcher-service

ARG VERSION=dev
ARG COMMIT=unknown

RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-s -w -X main.version=${VERSION} -X main.commit=${COMMIT}" \
    -o /bin/watcher-service \
    ./cmd/

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6

ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="vartrack-watcher-service" \
      org.opencontainers.image.description="VarTrack GitOps drift detector and self-healer" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/your-org/vartrack" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN apk add --no-cache ca-certificates tzdata wget && \
    addgroup -S nonroot && adduser -S -G nonroot nonroot

COPY --from=builder /bin/watcher-service /watcher-service

# Admin server (health, metrics)
EXPOSE 9091

USER nonroot:nonroot

ENTRYPOINT ["/watcher-service"]
