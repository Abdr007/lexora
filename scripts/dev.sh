#!/usr/bin/env bash
# Start / stop / check the local Lexora services.
#
# Identifying our own processes is harder than it looks, and getting it wrong is
# dangerous. Three projects share this machine and at least two of them run
# `python -m uvicorn app.main:app --port N`, so the command line does NOT identify
# the owner -- only the working directory does. An earlier version of this script
# matched on the command line alone, which meant `stop` could have killed a
# neighbouring project's API, and `status` did in fact report a neighbour's server
# as "Lexora, up" while Lexora was not running at all.
#
# So: a pid is ours only if its working directory is inside this repo. Every
# lookup below goes through that check, and `start` refuses to touch a port held
# by anyone else rather than pretending it is already running.
#
# Ports are deliberately non-default -- 7860, 7861 and 3000 belong to the other
# two projects on this machine.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${LEXORA_API_PORT:-7862}"
WEB_PORT="${LEXORA_WEB_PORT:-3020}"
API_DIR="$ROOT/apps/api"
WEB_DIR="$ROOT/apps/web"

# Every lsof call below ends in `|| true`. lsof exits non-zero when it finds nothing,
# which is the *normal* answer to "is this port free?" -- and under `set -euo pipefail`
# that non-zero status propagates out of the command substitution and aborts the whole
# script. That bug shipped once: `stop` killed the first service, hit a free port on the
# second lookup, and died silently, leaving a process running that it had just reported
# as stopped.

# The working directory of a pid, or empty when it cannot be read.
pid_cwd() { lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1 || true; }

# True only when the pid is running from inside this repo.
is_ours() {
  local cwd; cwd="$(pid_cwd "$1")"
  [ -n "$cwd" ] && [ "${cwd#"$ROOT"}" != "$cwd" ]
}

# Whoever is listening on a port, ours or not. Empty when the port is free.
port_pid() { lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -1 || true; }

# Our pid on a port. Empty when the port is free *or* held by another project --
# the two cases are distinguished by the caller, never conflated.
our_pid() {
  local pid; pid="$(port_pid "$1")"
  if [ -n "$pid" ] && is_ours "$pid"; then echo "$pid"; fi
}

api_pid() { our_pid "$API_PORT"; }
web_pid() { our_pid "$WEB_PORT"; }

# Name whoever is squatting on a port we wanted, and decline to interfere.
report_foreign() {
  local port="$1" var="$2" pid; pid="$(port_pid "$port")"
  [ -z "$pid" ] && return 1
  echo "  port $port is held by pid $pid running from $(pid_cwd "$pid")" >&2
  echo "  that is not this repo, so it will not be touched." >&2
  echo "  set LEXORA_${var}_PORT=<free port> and retry." >&2
  return 0
}

wait_for() {
  local url="$1" name="$2"
  for _ in $(seq 1 90); do
    if curl -sf "$url" >/dev/null 2>&1; then echo "  $name ready"; return 0; fi
    sleep 1
  done
  echo "  $name did NOT come up -- see /tmp/lexora_$name.log" >&2
  return 1
}

start_api() {
  if [ -n "$(api_pid)" ]; then echo "  api already running (pid $(api_pid))"; return 0; fi
  if report_foreign "$API_PORT" API; then return 1; fi
  cd "$ROOT"
  PYTHONPATH="$API_DIR" nohup "$API_DIR/.venv/bin/python" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$API_PORT" --log-level warning \
    > /tmp/lexora_api.log 2>&1 &
  wait_for "http://127.0.0.1:$API_PORT/api/health" "api"
}

start_web() {
  if [ -n "$(web_pid)" ]; then echo "  web already running (pid $(web_pid))"; return 0; fi
  if report_foreign "$WEB_PORT" WEB; then return 1; fi
  cd "$WEB_DIR"
  # Pass the API origin explicitly so the two ports can never drift apart.
  NEXT_PUBLIC_API_URL="${NEXT_PUBLIC_API_URL:-http://127.0.0.1:$API_PORT}" \
    nohup npm run dev > /tmp/lexora_web.log 2>&1 &
  wait_for "http://127.0.0.1:$WEB_PORT" "web"
}

# Signals only pids that passed is_ours(), and confirms they are gone.
#
# "Signal sent" is not "process stopped". The embedded Qdrant store allows a single
# writer, so an API that outlives its own `stop` keeps the lock and makes the whole
# integration suite skip -- which is exactly what happened before this waited.
kill_pid() {
  local pid="$1" name="$2"
  kill "$pid" 2>/dev/null || { echo "  $name (pid $pid) already gone"; return 0; }
  for _ in $(seq 1 100); do            # up to 10s for a graceful exit
    kill -0 "$pid" 2>/dev/null || { echo "  $name stopped (pid $pid)"; return 0; }
    sleep 0.1
  done
  echo "  $name (pid $pid) ignored SIGTERM; sending SIGKILL" >&2
  kill -9 "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || { echo "  $name killed (pid $pid)"; return 0; }
    sleep 0.1
  done
  echo "  $name (pid $pid) SURVIVED SIGKILL -- investigate before running tests" >&2
  return 1
}

stop_all() {
  local api web rc=0
  api="$(api_pid)"; web="$(web_pid)"
  [ -n "$api" ] && { kill_pid "$api" api || rc=1; }
  [ -n "$web" ] && { kill_pid "$web" web || rc=1; }
  [ -z "$api$web" ] && echo "  nothing of ours was running"
  echo "  nothing outside $ROOT was signalled"
  return $rc
}

show() {
  local name="$1" port="$2" mine foreign
  mine="$(our_pid "$port")"
  foreign="$(port_pid "$port")"
  if [ -n "$mine" ]; then
    echo "  $name  up       pid $mine  :$port"
  elif [ -n "$foreign" ]; then
    echo "  $name  FOREIGN  pid $foreign  :$port  <- $(pid_cwd "$foreign")"
  else
    echo "  $name  down     :$port"
  fi
}

case "${1:-start}" in
  start)  echo "starting Lexora"; start_api; start_web ;;
  api)    start_api ;;
  web)    start_web ;;
  stop)   stop_all ;;
  status)
    show api "$API_PORT"
    show web "$WEB_PORT"
    curl -sf "http://127.0.0.1:$API_PORT/api/health" 2>/dev/null | head -c 200 || true
    echo
    ;;
  *) echo "usage: scripts/dev.sh {start|stop|status|api|web}" >&2; exit 2 ;;
esac
