#!/bin/bash
# Stop the GPU monitor (kills wrapper + python child).
cd "$(dirname "$0")"
PID_FILE="run/monitor.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "no pid file"
  exit 0
fi

PID=$(cat "$PID_FILE")
if ! kill -0 "$PID" 2>/dev/null; then
  echo "process $PID not running"
  rm -f "$PID_FILE"
  exit 0
fi

# Kill the wrapper and any python child.
pkill -P "$PID" 2>/dev/null || true
kill "$PID" 2>/dev/null || true
sleep 1
if kill -0 "$PID" 2>/dev/null; then
  kill -9 "$PID" 2>/dev/null || true
fi
rm -f "$PID_FILE"
echo "stopped"
