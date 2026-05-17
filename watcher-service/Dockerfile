# ── Build stage ────────────────────────────────────────────────────────────────
FROM golang:1.26-alpine@sha256:91eda9776261207ea25fd06b5b7fed8d397dd2c0a283e77f2ab6e91bfa71079d AS builder

WORKDIR /build

COPY go.mod go.sum ./
RUN go mod download

COPY . .

ARG VERSION=dev
ARG COMMIT=unknown

RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags="-s -w -X main.version=${VERSION} -X main.commit=${COMMIT}" \
    -o watcher-service \
    ./cmd/

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM alpine:3.23@sha256:c69a6ff7c24d1ffa913798501d0e7104e0e9764e28eb44a930939f91ef829e64

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

COPY --from=builder /build/watcher-service /watcher-service

# Admin server (health, metrics)
EXPOSE 9091

USER nonroot:nonroot

ENTRYPOINT ["/watcher-service"]
