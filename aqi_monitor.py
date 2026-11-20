#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
空气质量监测 + 微信污染预警

数据源：Open-Meteo Air Quality API（免 API Key，CAMS 全球空气质量模型）
        https://air-quality-api.open-meteo.com
推送：Server酱 Turbo / PushPlus（按 Key 格式自动识别）

用法：
    python3 aqi_monitor.py            # 正常监测一次（cron 调用这个）
    python3 aqi_monitor.py --dry-run  # 只打印 AQI，不推送
    python3 aqi_monitor.py --test     # 强制发一条测试消息

退出码：0 正常；1 配置错误；2 数据获取失败；3 推送失败
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "aqi_monitor.log"
CST = timezone(timedelta(hours=8))

DEFAULT_CONFIG = {
    "city": "上海",
    "latitude": 31.2304,
    "longitude": 121.4737,
    "push_type": "auto",          # auto / serverchan / pushplus
    "send_key": "",               # 填 Server酱 SendKey 或 PushPlus Token
    "alert_aqi": 100,             # AQI >= 此值算"污染"，触发预警（100 = 轻度污染起点）
    "recover_notify": True,       # 污染转好后是否发"已恢复"通知
    "cooldown_minutes": 180,      # 同一等级最长静默时间，超过则重推（分钟）
    "daily_report": True,         # 是否每天固定时间发一条日报
    "daily_report_time": "08:00", # 日报发送时间（本地时间 HH:MM）
    "state_file": "aqi_state.json",
}

# ---------------------------------------------------------------- IAQI 计算
# HJ 633-2012 表1：浓度限值与 IAQI 对应关系
# 键为污染物名，值为 [(C_lo, C_hi, IAQI_lo, IAQI_hi), ...]
BREAKPOINTS = {
    "PM2.5": [(0, 35, 0, 50), (35, 75, 50, 100), (75, 115, 100, 150),
              (115, 150, 150, 200), (150, 250, 200, 300),
              (250, 350, 300, 400), (350, 500, 400, 500)],
    "PM10": [(0, 50, 0, 50), (50, 150, 50, 100), (150, 250, 100, 150),
             (250, 350, 150, 200), (350, 420, 200, 300),
             (420, 500, 300, 400), (500, 600, 400, 500)],
    "SO2": [(0, 50, 0, 50), (50, 150, 50, 100), (150, 475, 100, 150),
            (475, 800, 150, 200), (800, 1600, 200, 300),
            (1600, 2100, 300, 400), (2100, 2620, 400, 500)],
    "NO2": [(0, 40, 0, 50), (40, 80, 50, 100), (80, 180, 100, 150),
            (180, 280, 150, 200), (280, 565, 200, 300),
            (565, 750, 300, 400), (750, 940, 400, 500)],
    "CO": [(0, 2, 0, 50), (2, 4, 50, 100), (4, 14, 100, 150),
           (14, 24, 150, 200), (24, 36, 200, 300),
           (36, 48, 300, 400), (48, 60, 400, 500)],
    "O3": [(0, 160, 0, 50), (160, 200, 50, 100), (200, 300, 100, 150),
           (300, 400, 150, 200), (400, 800, 200, 300),
           (800, 1000, 300, 400), (1000, 1200, 400, 500)],
}

# 等级：(AQI 下限(含), AQI 上限(含), 中文名, 提示)
LEVELS = [
    (0, 50, "优", "空气质量令人满意，可正常活动。"),
    (51, 100, "良", "空气质量可接受，极少数敏感人群应减少户外活动。"),
    (101, 150, "轻度污染", "易感人群症状轻度加剧，健康人群出现刺激症状；儿童、老人、心肺疾病患者减少长时间户外活动。"),
    (151, 200, "中度污染", "进一步加剧易感人群症状，可能对健康人群心脏、呼吸系统有影响；一般人群减少户外活动。"),
    (201, 300, "重度污染", "健康人群运动耐受力降低，有明显强烈症状；建议停止户外活动，外出佩戴口罩。"),
    (301, 500, "严重污染", "健康人群运动耐受力降低，有明显强烈症状，提前出现某些疾病；应避免户外活动，关闭门窗。"),
]


def iaqi(pollutant: str, conc: float) -> float:
    """按国标分段线性公式计算单项空气质量分指数。"""
    if conc is None or conc < 0:
        return 0.0
    for c_lo, c_hi, i_lo, i_hi in BREAKPOINTS[pollutant]:
        if conc <= c_hi:
            return round((i_hi - i_lo) / (c_hi - c_lo) * (conc - c_lo) + i_lo, 1)
    # 超上限按最高档封顶
    return 500.0


def level_of(aqi: float):
    for lo, hi, name, tip in LEVELS:
        if aqi <= hi:
            return name, tip
    return LEVELS[-1][2], LEVELS[-1][3]


def level_rank(aqi: float) -> int:
    """等级序号，越大越严重。"""
    for idx, (lo, hi, name, tip) in enumerate(LEVELS):
        if aqi <= hi:
            return idx
    return len(LEVELS) - 1


# ---------------------------------------------------------------- 数据获取
def _get_json(url: str, retries: int = 3) -> dict:
    """带重试退避的 GET，应对免费额度的偶发 503/超时。"""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "aqi-monitor/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"请求失败（重试 {retries} 次）：{last_err}")


def fetch_air(lat: float, lon: float) -> dict:
    url = (
        "https://air-quality-api.open-meteo.com/v1/air-quality"
        f"?latitude={lat}&longitude={lon}"
        "&current=pm2_5,pm10,nitrogen_dioxide,sulphur_dioxide,ozone,carbon_monoxide"
        "&timezone=Asia%2FShanghai"
    )
    payload = _get_json(url)
    cur = payload.get("current") or {}
    if not cur:
        raise RuntimeError("接口未返回 current 数据")
    return {
        "time": cur.get("time"),
        "PM2.5": cur.get("pm2_5"),
        "PM10": cur.get("pm10"),
        "NO2": cur.get("nitrogen_dioxide"),
        "SO2": cur.get("sulphur_dioxide"),
        "O3": cur.get("ozone"),
        # 接口 CO 单位 μg/m³，国标分段用 mg/m³，换算后再算
        "CO": (cur.get("carbon_monoxide") or 0) / 1000.0,
    }


def compute_aqi(data: dict) -> dict:
    items = {}
    for p in BREAKPOINTS:
        items[p] = {"conc": data.get(p), "iaqi": iaqi(p, data.get(p))}
    aqi = max(v["iaqi"] for v in items.values())
    aqi = round(aqi)
    primary = "-"
    if aqi > 50:
        # 首要污染物：分指数最高的那项（并列时一并列出）
        top = max(v["iaqi"] for v in items.values())
        primary = "、".join(p for p, v in items.items() if v["iaqi"] == top)
    name, tip = level_of(aqi)
    return {"aqi": aqi, "level": name, "primary": primary, "tip": tip,
            "items": items, "time": data.get("time")}


# ---------------------------------------------------------------- 预报
def fetch_forecast(lat: float, lon: float) -> list[dict]:
    """取当天逐小时预报，返回每小时的 AQI 序列。"""
    url = (
        "https://air-quality-api.open-meteo.com/v1/air-quality"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=pm2_5,pm10,nitrogen_dioxide,sulphur_dioxide,ozone,carbon_monoxide"
        "&forecast_days=1&timezone=Asia%2FShanghai"
    )
    payload = _get_json(url)
    h = payload.get("hourly") or {}
    times = h.get("time") or []
    series = []
    for i, t in enumerate(times):
        row = {
            "PM2.5": h["pm2_5"][i],
            "PM10": h["pm10"][i],
            "NO2": h["nitrogen_dioxide"][i],
            "SO2": h["sulphur_dioxide"][i],
            "O3": h["ozone"][i],
            "CO": (h["carbon_monoxide"][i] or 0) / 1000.0,
        }
        res = compute_aqi(dict(row, time=t))
        series.append({"time": t, "aqi": res["aqi"], "level": res["level"],
                       "primary": res["primary"]})
    # 只保留当前时刻之后的时段（日报关心的是"接下来会怎样"），若已到日终则保留全天
    now_key = datetime.now(CST).strftime("%Y-%m-%dT%H")
    future = [s for s in series if s["time"] >= now_key]
    return future or series


def summarize_forecast(series: list[dict]) -> dict:
    if not series:
        return {}
    peak = max(series, key=lambda x: x["aqi"])
    low = min(series, key=lambda x: x["aqi"])
    return {"peak": peak, "low": low, "max_aqi": peak["aqi"], "min_aqi": low["aqi"]}


# ---------------------------------------------------------------- 推送
def detect_push_type(key: str) -> str:
    if key.lower().startswith("sct"):
        return "serverchan"
    if re.fullmatch(r"[0-9a-fA-F]{32}", key):
        return "pushplus"
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", key):
        return "pushplus"
    return "serverchan"


def push_message(cfg: dict, title: str, body: str) -> None:
    key = (cfg.get("send_key") or "").strip()
    if not key:
        raise RuntimeError("config.json 里的 send_key 还没填")
    ptype = cfg.get("push_type", "auto")
    if ptype == "auto":
        ptype = detect_push_type(key)

    if ptype == "serverchan":
        url = f"https://sctapi.ftqq.com/{key}.send"
        data = urllib.parse.urlencode({"title": title, "desp": body}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
    else:  # pushplus
        url = "https://www.pushplus.plus/send"
        payload = json.dumps({"token": key, "title": title, "content": body,
                              "template": "markdown"}).encode()
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    try:
        j = json.loads(raw)
        code = j.get("code", j.get("errno", 0))
        if code not in (0, "0", 200):
            raise RuntimeError(f"推送接口返回异常：{raw[:200]}")
    except json.JSONDecodeError:
        raise RuntimeError(f"推送接口返回非 JSON：{raw[:200]}")


# ---------------------------------------------------------------- 状态与防骚扰
def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            pass
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def log(msg: str) -> None:
    line = f"[{datetime.now(CST):%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 主流程
def build_body(cfg: dict, res: dict, kind: str) -> str:
    d = res["items"]
    lines = [
        f"**{cfg['city']}空气质量 {res['level']}（AQI {res['aqi']}）**",
        "",
        f"- 发布时间：{res['time']}",
        f"- 首要污染物：{res['primary']}",
        "",
        "| 污染物 | 浓度 | 分指数 IAQI |",
        "|---|---|---|",
    ]
    for p, v in d.items():
        unit = "mg/m³" if p == "CO" else "μg/m³"
        conc = "-" if v["conc"] is None else f"{v['conc']:.1f}"
        lines.append(f"| {p} | {conc} {unit} | {v['iaqi']} |")
    lines += ["", f"**提示**：{res['tip']}"]
    if kind == "recover":
        lines.insert(0, "空气质量已回落至达标范围。")
    return "\n".join(lines)


def build_daily_body(cfg: dict, res: dict, forecast: dict) -> str:
    d = res["items"]
    lines = [
        f"**{cfg['city']}空气质量日报 · {datetime.now(CST):%m月%d日}**",
        "",
        f"- 当前：AQI **{res['aqi']}**，{res['level']}",
        f"- 首要污染物：{res['primary']}",
        f"- 数据时间：{res['time']}",
    ]
    if forecast:
        peak = forecast["peak"]
        base = (f"**今日后续时段**：最高 AQI **{forecast['max_aqi']}**"
                f"（{peak['level']}），出现在 {peak['time'][11:16]}")
        if peak["time"] != forecast["low"]["time"]:
            base += f"；最低 AQI {forecast['min_aqi']}（{forecast['low']['level']}）"
        lines += ["", base + "。"]
    lines += [
        "",
        "| 污染物 | 当前浓度 | 分指数 |",
        "|---|---|---|",
    ]
    for p, v in d.items():
        unit = "mg/m³" if p == "CO" else "μg/m³"
        conc = "-" if v["conc"] is None else f"{v['conc']:.1f}"
        lines.append(f"| {p} | {conc} {unit} | {v['iaqi']} |")
    lines += [
        "",
        f"**建议**：{res['tip']}",
        "",
        f"_今日仅在污染时另行预警（AQI ≥ {cfg.get('alert_aqi', 100)}）。_",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    ap.add_argument("--test", action="store_true", help="强制发测试消息")
    ap.add_argument("--test-daily", action="store_true", help="强制发一条日报（验证格式）")
    args = ap.parse_args()

    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
        except Exception as e:
            log(f"config.json 解析失败，使用默认配置：{e}")
    else:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
        log(f"已生成默认配置：{CONFIG_PATH}")

    try:
        data = fetch_air(float(cfg["latitude"]), float(cfg["longitude"]))
    except Exception as e:
        log(f"数据获取失败：{e}")
        return 2

    res = compute_aqi(data)
    log(f"{cfg['city']} AQI={res['aqi']} 等级={res['level']} "
        f"首要污染物={res['primary']} 发布={res['time']}")

    if args.dry_run:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    if args.test:
        try:
            push_message(cfg, f"【测试】{cfg['city']}空气质量 AQI {res['aqi']} {res['level']}",
                         build_body(cfg, res, "test"))
            log("测试消息已发送")
            return 0
        except Exception as e:
            log(f"推送失败：{e}")
            return 3

    if args.test_daily:
        try:
            fc = summarize_forecast(fetch_forecast(float(cfg["latitude"]), float(cfg["longitude"])))
        except Exception as e:
            fc = {}
            log(f"预报获取失败：{e}")
        try:
            push_message(cfg,
                         f"【空气质量日报】{cfg['city']} AQI {res['aqi']} {res['level']}",
                         build_daily_body(cfg, res, fc))
            log("日报示例已发送")
            return 0
        except Exception as e:
            log(f"推送失败：{e}")
            return 3

    state_path = BASE_DIR / cfg.get("state_file", "aqi_state.json")
    state = load_state(state_path)
    now_ts = datetime.now(CST).timestamp()
    alert_aqi = float(cfg.get("alert_aqi", 100))
    cooldown = float(cfg.get("cooldown_minutes", 180)) * 60

    is_polluted = res["aqi"] >= alert_aqi
    last_rank = int(state.get("last_push_rank", -1))
    last_ts = float(state.get("last_push_ts", 0))
    last_was_polluted = bool(state.get("last_polluted", False))

    # ---- 每日日报：到点且今天还没发过 → 发（不论当前是否污染）----
    now_dt = datetime.now(CST)
    today_str = now_dt.strftime("%Y-%m-%d")
    daily_sent = False
    if cfg.get("daily_report", True) and state.get("last_daily_report_date") != today_str:
        try:
            rh, rm = [int(x) for x in str(cfg.get("daily_report_time", "08:00")).split(":")]
        except Exception:
            rh, rm = 8, 0
        if now_dt.hour * 60 + now_dt.minute >= rh * 60 + rm:
            try:
                fc = summarize_forecast(fetch_forecast(float(cfg["latitude"]),
                                                       float(cfg["longitude"])))
            except Exception as e:
                fc = {}
                log(f"预报获取失败，日报仅含当前数据：{e}")
            try:
                push_message(cfg,
                             f"【空气质量日报】{cfg['city']} AQI {res['aqi']} {res['level']}",
                             build_daily_body(cfg, res, fc))
                log("已推送（daily）：每日空气质量日报")
                daily_sent = True
                state["last_daily_report_date"] = today_str
            except Exception as e:
                log(f"日报推送失败：{e}")
                return 3

    title = body = None
    kind = None
    if is_polluted:
        cur_rank = level_rank(res["aqi"])
        # 首次污染 / 等级加重 / 静默超时 → 推送（本轮已发日报则不再重复）
        should = (not last_was_polluted) or (cur_rank > last_rank) or (now_ts - last_ts >= cooldown)
        if should and not daily_sent:
            title = f"【空气污染预警】{cfg['city']} AQI {res['aqi']} {res['level']}"
            body = build_body(cfg, res, "alert")
            kind = "alert"
        elif daily_sent:
            log("本轮已发日报（其中已包含当前 AQI），污染预警不重复推送")
        else:
            log("仍在污染但未达重推条件（等级未加重且在静默期内），跳过")
    else:
        if last_was_polluted and cfg.get("recover_notify", True) and not daily_sent:
            title = f"【空气已恢复】{cfg['city']} AQI {res['aqi']} {res['level']}"
            body = build_body(cfg, res, "recover")
            kind = "recover"
        elif daily_sent and last_was_polluted:
            log("本轮已发日报，恢复通知不重复推送")
        else:
            log("空气质量达标，无需告警")

    if title:
        try:
            push_message(cfg, title, body)
            log(f"已推送（{kind}）：{title}")
        except Exception as e:
            log(f"推送失败：{e}")
            return 3
        state["last_push_rank"] = level_rank(res["aqi"])
        state["last_push_ts"] = now_ts

    # 日报已内含当前 AQI，若此刻正污染，把静默计时起点前移，
    # 避免下一轮又立刻补一条预警（除非等级继续加重）
    if daily_sent and is_polluted:
        state["last_push_rank"] = level_rank(res["aqi"])
        state["last_push_ts"] = now_ts

    state["last_polluted"] = is_polluted
    state["last_aqi"] = res["aqi"]
    state["last_level"] = res["level"]
    state["last_check"] = datetime.now(CST).isoformat(timespec="seconds")
    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
