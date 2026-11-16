#!/bin/bash
# varTrack demo
# Usage:  ./run.sh          — build and start the demo (the seed runs once, and leaves what you change)
#         ./run.sh logs     — tail all service logs
#         ./run.sh down     — stop and remove the containers (the data stays: the next start finds the demo seeded)
#         ./run.sh reset    — stop, remove the containers AND the data: the next start seeds again from nothing
set -euo pipefail

cd "$(dirname "$0")"
COMPOSE="docker compose -f docker-compose.yml"
CMD=${1:-up}

# The oldest Compose this demo is checked on: 2.20.0, which reads docker-compose.yml and has `docker compose wait`, which this
# script uses. (A CI job runs the real releases to check it: .github/workflows/ci.yml, compose-minimum.)
MIN_COMPOSE=2.20.0
found=$(docker compose version --short 2>/dev/null | sed 's/^v//') || found=""
if [ -z "$found" ]; then
    echo "Docker Compose ${MIN_COMPOSE} or newer is needed, and 'docker compose' (with a space) was not found." >&2
    echo "The old 'docker-compose' (Compose V1, often a distribution's package) does not run this demo: install Docker's own" >&2
    echo "packages (https://docs.docker.com/compose/install/) or Docker Desktop." >&2
    exit 1
fi
if [ "$(printf '%s\n%s\n' "$MIN_COMPOSE" "$found" | sort -V | head -n 1)" != "$MIN_COMPOSE" ]; then
    echo "Docker Compose ${MIN_COMPOSE} or newer is needed (found ${found}). Update it: https://docs.docker.com/compose/install/" >&2
    exit 1
fi

case "$CMD" in
  up)
    echo "▶  Building and starting the varTrack demo…"
    echo "   This takes 2–4 minutes on the first run (Docker builds)."
    echo ""
    $COMPOSE up --build --detach --remove-orphans
    # demo-seed exits 0 when the demo is ready, and says where to go in its last lines.
    $COMPOSE wait demo-seed >/dev/null
    $COMPOSE logs --no-log-prefix --tail 8 demo-seed
    ;;

  logs)
    shift
    $COMPOSE logs -f "$@"
    ;;

  down)
    $COMPOSE down
    ;;

  reset)
    $COMPOSE down -v --remove-orphans
    echo "All containers and data removed: the next start seeds the demo again."
    ;;

  *)
    echo "Usage: $0 [up|logs|down|reset]"
    exit 1
    ;;
esac
