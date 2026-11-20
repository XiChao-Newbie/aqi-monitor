#!/usr/bin/env bash
# 一键安装定时任务：每 30 分钟监测一次
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="/opt/python3.12/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

JOB="*/30 * * * * cd $DIR && $PY aqi_monitor.py >> $DIR/cron.err 2>&1"

# 先移除旧的本脚本任务，避免重复
( crontab -l 2>/dev/null | grep -v -F "$DIR/aqi_monitor.py" ) > /tmp/aqi_cron_now || true
echo "$JOB" >> /tmp/aqi_cron_now
crontab /tmp/aqi_cron_now
rm -f /tmp/aqi_cron_now

echo "已安装 cron，每 30 分钟运行一次："
crontab -l | grep -F "aqi_monitor.py"
echo
echo "提示：cron 用的是系统默认时区，请确认主机时区为 Asia/Shanghai（timedatectl | grep 'Time zone'）"
