#!/usr/bin/env bash
# Start / stop / check the local Lexora services.
#
# Ports are deliberately non-default: 7860 and 3000 are commonly taken by other
# projects on a developer machine, and silently binding a neighbour's port is a
# confusing failure. Everything here matches on the repo path so it can never
# signal another project's process.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${LEXORA_API_PORT:-7861}"
WEB_PORT="${LEXORA_WEB_PORT:-3020}"
API_PATTERN="$ROOT/apps/api"
WEB_PATTERN="$ROOT/apps/web"

api_pid() { pgrep -f "uvicorn app.main:app.*--port $API_PORT" 2>/dev/null | head -1; }
web_pid() { pgrep -f "next dev --port $WEB_PORT" 2>/dev/null | head -1; }

wait_for() {
  local url="$1" name="$2"
  for _ in $(seq 1 90); do
    if curl -sf "$url" >/dev/null 2>&1; then echo "  $name ready"; return 0; fi
    sleep 1
  done
  echo "  $name did NOT come up" >&2
  return 1
}

start_api() {
  if [ -n "$(api_pid)" ]; then echo "  api already running (pid $(api_pid))"; return; fi
  cd "$ROOT"
  PYTHONPATH="$API_PATTERN" nohup "$API_PATTERN/.venv/bin/python" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$API_PORT" --log-level warning \
    > /tmp/lexora_api.log 2>&1 &
  wait_for "http://127.0.0.1:$API_PORT/api/health" "api"
}

start_web() {
  if [ -n "$(web_pid)" ]; then echo "  web already running (pid $(web_pid))"; return; fi
  cd "$WEB_PATTERN"
  nohup npm run dev > /tmp/lexora_web.log 2>&1 &
  wait_for "http://127.0.0.1:$WEB_PORT" "web"
}

case "${1:-start}" in
  start)  echo "starting Lexora"; start_api; start_web ;;
  api)    start_api ;;
  web)    start_web ;;
  stop)
    for pid in $(api_pid) $(web_pid); do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
    echo "  stopped (other projects untouched)"
    ;;
  status)
    echo "  api  $( [ -n "$(api_pid)" ] && echo "up   pid $(api_pid)" || echo down ) :$API_PORT"
    echo "  web  $( [ -n "$(web_pid)" ] && echo "up   pid $(web_pid)" || echo down ) :$WEB_PORT"
    curl -sf "http://127.0.0.1:$API_PORT/api/health" 2>/dev/null | head -c 200 || true
    echo
    ;;
  *) echo "usage: scripts/dev.sh {start|stop|status|api|web}" >&2; exit 2 ;;
esac
