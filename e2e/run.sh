#!/bin/bash
# VarTrack E2E quick-start
# Usage:  ./run.sh          — build + run full demo
#         ./run.sh logs     — tail all service logs
#         ./run.sh down     — stop and remove containers
#         ./run.sh clean    — stop, remove containers AND volumes (full reset)
#         ./run.sh results  — re-run only the results query (no ETL)
set -euo pipefail

COMPOSE="docker compose -f $(dirname "$0")/docker-compose.yml"
CMD=${1:-up}

case "$CMD" in
  up)
    echo "▶  Building and starting VarTrack E2E demo…"
    echo "   This takes 2–4 minutes on first run (Docker builds)."
    echo ""
    $COMPOSE build --parallel
    $COMPOSE up --remove-orphans \
        --exit-code-from e2e-demo \
        2>&1 | tee /tmp/vartrack-e2e.log | grep \
              -E "PHASE|✓|✗|⚠|MongoDB|ZooKeeper|task_id|Tampered|Healed|complete|ERROR|error" \
              --color=always || true
    echo ""
    echo "Full log saved to /tmp/vartrack-e2e.log"
    echo "Run './run.sh down' when finished."
    ;;

  logs)
    shift
    $COMPOSE logs -f "$@"
    ;;

  down)
    $COMPOSE down
    ;;

  clean)
    $COMPOSE down -v --remove-orphans
    echo "All containers and volumes removed."
    ;;

  results)
    # Re-run just the Python result queries against the running stack.
    $COMPOSE run --rm --no-deps e2e-demo bash -c "
      python3 - <<'EOF'
from pymongo import MongoClient
c = MongoClient('mongodb://mongo:27017', serverSelectionTimeoutMS=5000)
docs = list(c.vartrack.variables.find({}, {'_id':0}))
print(f'MongoDB: {len(docs)} document(s)')
for d in docs: print(' ', {k:v for k,v in d.items() if not k.startswith('_vt_')})
c.close()

from kazoo.client import KazooClient
zk = KazooClient(hosts='zookeeper:2181', timeout=5)
zk.start(timeout=5)
if zk.exists('/vartrack/demo'):
    kids = zk.get_children('/vartrack/demo')
    print(f'ZooKeeper /vartrack/demo: {len(kids)} node(s): {kids[:5]}')
else:
    print('ZooKeeper: /vartrack/demo not found')
zk.stop()
EOF
"
    ;;

  *)
    echo "Usage: $0 [up|logs|down|clean|results]"
    exit 1
    ;;
esac
