#!/usr/bin/env bash
# 停止 AQI 常驻守护进程
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$DIR/daemon.pid" ]; then
  echo "没有 pid 文件，可能未在运行"
  exit 0
fi
PID="$(cat "$DIR/daemon.pid")"
if kill -0 "$PID" 2>/dev/null; then
  kill -TERM "$PID"
  for i in $(seq 1 20); do
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.5
  done
  kill -0 "$PID" 2>/dev/null && kill -KILL "$PID" 2>/dev/null
  echo "已停止 PID=$PID"
else
  echo "进程 $PID 不存在，清理 pid 文件"
  rm -f "$DIR/daemon.pid"
fi
