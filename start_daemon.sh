#!/usr/bin/env bash
# 后台启动 AQI 常驻守护进程（脱离当前终端，不受会话影响）
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="/opt/python3.12/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

if [ -f "$DIR/daemon.pid" ] && kill -0 "$(cat "$DIR/daemon.pid")" 2>/dev/null; then
  echo "已在运行，PID=$(cat "$DIR/daemon.pid")"
  exit 0
fi

cd "$DIR"
setsid nohup "$PY" aqi_daemon.py >> "$DIR/daemon.log" 2>&1 < /dev/null &
sleep 2

if [ -f "$DIR/daemon.pid" ] && kill -0 "$(cat "$DIR/daemon.pid")" 2>/dev/null; then
  echo "已启动，PID=$(cat "$DIR/daemon.pid")"
  tail -5 "$DIR/daemon.log"
else
  echo "启动失败，最近日志："
  tail -20 "$DIR/daemon.log" 2>/dev/null || echo "(无日志)"
  exit 1
fi
