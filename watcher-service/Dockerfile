# ── Build stage ────────────────────────────────────────────────────────────────
FROM golang:1.26-alpine@sha256:f85330846cde1e57ca9ec309382da3b8e6ae3ab943d2739500e08c86393a21b1 AS builder

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
FROM alpine:3.20@sha256:a4f4213abb84c497377b8544c81b3564f313746700372ec4fe84653e4fb03805

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
