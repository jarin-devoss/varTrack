# ─── Stage 1: Build ──────────────────────────────────────────────────────────
FROM golang:1.26-alpine@sha256:f85330846cde1e57ca9ec309382da3b8e6ae3ab943d2739500e08c86393a21b1 AS builder

ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown

RUN apk add --no-cache ca-certificates tzdata

WORKDIR /src

# Cache dependency downloads separately from source code.
COPY go.mod go.sum ./
RUN go mod download

COPY . .

# Build a statically-linked binary with all debugging symbols stripped.
# Version metadata is embedded via ldflags for runtime inspection.
RUN CGO_ENABLED=0 GOOS=linux \
    go build \
      -ldflags="-s -w \
        -X main.version=${VERSION} \
        -X main.commit=${COMMIT} \
        -X main.buildDate=${BUILD_DATE}" \
      -trimpath \
      -o /bin/gateway-service \
      ./cmd/...

# ─── Stage 2: Runtime ────────────────────────────────────────────────────────
FROM alpine:3.23@sha256:c69a6ff7c24d1ffa913798501d0e7104e0e9764e28eb44a930939f91ef829e64

ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="vartrack-gateway-service" \
      org.opencontainers.image.description="VarTrack HTTP ingress gateway — webhook receiver and gRPC forwarder" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/your-org/vartrack" \
      org.opencontainers.image.licenses="Apache-2.0"

# Alpine includes wget and ca-certificates by default; add tzdata for correct timestamps.
RUN apk upgrade --no-cache && \
    apk add --no-cache tzdata && \
    addgroup -S nonroot && adduser -S -G nonroot nonroot

COPY --from=builder /bin/gateway-service /gateway-service

# The CUE config file must be mounted or baked in at deploy time.
# Example: docker run -v ./config.cue:/config.cue ...
WORKDIR /

# Default ports: gateway HTTP 5657, admin/metrics 9090.
EXPOSE 5657 9090

USER nonroot:nonroot

ENTRYPOINT ["/gateway-service"]
