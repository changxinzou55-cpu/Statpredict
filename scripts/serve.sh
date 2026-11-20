#!/bin/sh
# Runs the server on the given port and starts it again if it ever stops.
cd "$(dirname "$0")/.." || exit 1
PORT="${1:-8100}"
while true; do
  python3 -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
  echo "$(date -u +%FT%TZ) server on port $PORT stopped (exit $?), starting again in 3 seconds" >&2
  sleep 3
done
