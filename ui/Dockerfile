# ─── Stage 1: Build ──────────────────────────────────────────────────────────
FROM node:22-alpine AS builder

WORKDIR /src

# Cache dependency installs separately from source code.
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
RUN npm run build

# ─── Stage 2: Runtime ────────────────────────────────────────────────────────
FROM nginx:1.27-alpine

LABEL org.opencontainers.image.title="vartrack-ui" \
      org.opencontainers.image.description="VarTrack web UI — static SPA with a reverse proxy to the gateway, orchestrator and watcher"

# Upstreams the /api/<service> paths are proxied to, and the DNS server used
# to resolve them (Docker's embedded DNS by default). The nginx image renders
# /etc/nginx/templates/*.template with these values at container start.
ENV GATEWAY_URL=http://gateway:5657 \
    ORCHESTRATOR_URL=http://orchestrator-api:8000 \
    WATCHER_URL=http://watcher:9091 \
    RESOLVER=127.0.0.11

COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=builder /src/dist /usr/share/nginx/html

EXPOSE 8080
