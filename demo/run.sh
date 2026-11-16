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
