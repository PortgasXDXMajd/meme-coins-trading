#!/usr/bin/env bash
# Run the desk without Docker: keeps the Mac awake and restarts the desk if it crashes.
#   Start:  nohup scripts/run_overnight.sh >/dev/null 2>&1 &
#   Stop:   pkill -TERM -f "desk run"      (a clean stop ends the restart loop)
set -u
cd "$(dirname "$0")/.."
mkdir -p logs
unset VIRTUAL_ENV

while true; do
  caffeinate -ims uv run desk run >>logs/overnight.out 2>&1
  code=$?
  if [ "$code" -eq 0 ]; then
    echo "$(date '+%F %T') desk stopped cleanly" >>logs/overnight.out
    break
  fi
  echo "$(date '+%F %T') desk exited with code $code, restarting in 15s" >>logs/overnight.out
  sleep 15
done
