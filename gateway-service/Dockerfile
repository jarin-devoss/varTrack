# ─── Stage 1: Build ──────────────────────────────────────────────────────────
FROM golang:1.27-alpine@sha256:f92b6ef800e499660581efdabdf25d9d817a9d124eaf900924f0504e7e27e12d AS builder

ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_DATE=unknown

RUN apk add --no-cache ca-certificates tzdata

# The build context is the repository root: the gateway builds against the
# shared module in libs/go (its go.mod replaces vartrack/libs with ../libs/go).
WORKDIR /src

# Cache dependency downloads separately from source code.
COPY libs/go/go.mod libs/go/go.sum libs/go/
COPY gateway-service/go.mod gateway-service/go.sum gateway-service/
WORKDIR /src/gateway-service
RUN go mod download

WORKDIR /src
COPY libs/go libs/go
COPY gateway-service gateway-service
WORKDIR /src/gateway-service

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
FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6

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
