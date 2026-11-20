#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AQI 常驻守护进程

容器内没有 systemd / cron 守护进程，所以用一个自带循环的进程来定时监测。
- 启动即执行一次检查，之后按 config.json 的 interval_minutes 间隔循环
- 单实例锁，重复启动会被拒绝
- 每次检查重新读取 config.json，改阈值不用重启
- 异常不会让进程退出，记录后继续下一次

用法：
    ./start_daemon.sh     # 后台启动
    ./stop_daemon.sh      # 停止
"""

import fcntl
import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PID_PATH = BASE_DIR / "daemon.pid"
LOG_PATH = BASE_DIR / "daemon.log"
CST = timezone(timedelta(hours=8))

sys.path.insert(0, str(BASE_DIR))
import aqi_monitor as core  # noqa: E402

_running = True


def dlog(msg: str) -> None:
    line = f"[{datetime.now(CST):%Y-%m-%d %H:%M:%S}] [daemon] {msg}"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_interval() -> float:
    """每次循环重读配置，支持热更新间隔。"""
    try:
        cfg = json.loads((BASE_DIR / "config.json").read_text("utf-8"))
        return max(1.0, float(cfg.get("interval_minutes", 30)))
    except Exception:
        return 30.0


def handle_stop(signum, frame):
    global _running
    _running = False
    dlog(f"收到信号 {signum}，准备退出")


def main() -> int:
    # 单实例锁
    lock_fp = open(BASE_DIR / "daemon.lock", "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("已有守护进程在运行，本次启动被拒绝")
        return 1

    PID_PATH.write_text(str(os.getpid()), "utf-8")
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGHUP, handle_stop)

    dlog(f"启动，PID={os.getpid()}，间隔 {read_interval():.0f} 分钟")

    while _running:
        try:
            sys.argv = ["aqi_monitor.py"]  # 保证 argparse 拿到干净参数
            code = core.main()
            if code not in (0, None):
                dlog(f"本轮检查返回非零退出码 {code}")
        except Exception:
            dlog("本轮检查异常：\n" + traceback.format_exc())

        # 分段睡眠，便于收到信号后及时退出
        interval = read_interval() * 60
        elapsed = 0.0
        while _running and elapsed < interval:
            step = min(5.0, interval - elapsed)
            time.sleep(step)
            elapsed += step

    dlog("已退出")
    try:
        PID_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
