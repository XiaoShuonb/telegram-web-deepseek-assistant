#!/bin/sh

set -u

PROJECT_DIR="${APP_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
DATA_DIR="$PROJECT_DIR/data"
RUNNER_LOG="$DATA_DIR/runner.log"
RUNNER_PID="$DATA_DIR/runner.pid"
RUNNER_LOCK="$DATA_DIR/runner.lock"
PYTHON="$PROJECT_DIR/.venv/bin/python"

umask 077
mkdir -p "$DATA_DIR"
cd "$PROJECT_DIR" || exit 1

exec 9>"$RUNNER_LOCK"
/usr/bin/flock -n 9 || exit 0

if [ -f "$RUNNER_LOG" ] && [ "$(wc -c < "$RUNNER_LOG")" -gt 10485760 ]; then
  mv "$RUNNER_LOG" "$RUNNER_LOG.1"
fi

printf '%s\n' "$$" > "$RUNNER_PID"
child_pid=""

cleanup() {
  if [ -n "$child_pid" ]; then
    kill "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  rm -f "$RUNNER_PID"
  exit 0
}

trap cleanup INT TERM

export PLAYWRIGHT_BROWSERS_PATH="$DATA_DIR/ms-playwright"
export LD_LIBRARY_PATH="$DATA_DIR/chromium-libs/usr/lib/x86_64-linux-gnu"

while :; do
  printf '%s runner: starting tgdeepseek\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$RUNNER_LOG"
  "$PYTHON" -m tgdeepseek run >> "$RUNNER_LOG" 2>&1 &
  child_pid="$!"
  wait "$child_pid"
  status="$?"
  child_pid=""
  printf '%s runner: tgdeepseek exited with status %s; restarting in 8 seconds\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$status" >> "$RUNNER_LOG"
  sleep 8
done
