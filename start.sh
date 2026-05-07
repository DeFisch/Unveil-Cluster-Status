#!/bin/bash
# Start the GPU monitor as a background daemon with auto-restart on crash.
set -e
cd "$(dirname "$0")"

PID_FILE="run/monitor.pid"
LOG_FILE="run/monitor.log"
mkdir -p run

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "monitor already running (pid $(cat "$PID_FILE"))"
  exit 0
fi

# Wrapper loop: restart Python on crash, but back off to avoid busy loops.
nohup bash -c '
  cd "'"$(pwd)"'"
  while true; do
    /home/dfeng8/miniconda3/bin/python3 monitor.py
    rc=$?
    echo "[$(date -Iseconds)] monitor exited rc=$rc, restarting in 30s" >&2
    sleep 30
  done
' >> "$LOG_FILE" 2>&1 &

echo $! > "$PID_FILE"
sleep 1
if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "started monitor (pid $(cat "$PID_FILE")), logging to $LOG_FILE"
else
  echo "failed to start; check $LOG_FILE"
  rm -f "$PID_FILE"
  exit 1
fi
