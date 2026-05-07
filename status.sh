#!/bin/bash
cd "$(dirname "$0")"
PID_FILE="run/monitor.pid"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  PID=$(cat "$PID_FILE")
  echo "running (pid $PID)"
  ps -o pid,etime,command --ppid "$PID" 2>/dev/null
else
  echo "not running"
  exit 1
fi

if [ -f data/samples.jsonl ]; then
  COUNT=$(wc -l < data/samples.jsonl)
  LAST=$(tail -1 data/samples.jsonl 2>/dev/null | head -c 30)
  echo "samples: $COUNT total"
  echo "last: $LAST..."
fi

echo
echo "tail of log:"
tail -5 run/monitor.log 2>/dev/null
