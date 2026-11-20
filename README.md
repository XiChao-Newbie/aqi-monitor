# 空气质量监测 + 微信提醒

两套机制分工，避免互相重复：

| 职责 | 由谁负责 | 通道 |
|---|---|---|
| **每天一条日报** | 平台定时任务「上海空气质量日报」 | 任务回复直达（独立环境，不依赖本机） |
| **污染预警 / 恢复通知** | 本机守护进程 `aqi_daemon.py` | 微信（Server酱） |

## 为什么这么拆

实测发现：平台的定时任务跑在**独立的云电脑环境**里，看不到这台机器上的任何文件（我让它在项目目录写标记文件，本机完全搜不到）。所以它没法调用本机脚本，只能自己抓数据算 AQI，然后把日报作为任务回复送达。

反过来，本机守护进程受限于容器重启后不会自动拉起。日报这种"每天必须有"的内容交给平台任务，才真正扛得住重启；污染预警需要高频，也只能放在本机。

## 平台定时任务

- 名称：上海空气质量日报
- 时间：每天 08:00（`0 8 * * *`）
- 环境：云端（云电脑）
- 内容：自己在云端请求 Open-Meteo，用内嵌的 HJ 633-2012 分段线性公式算 AQI，输出日报正文
- 不依赖任何本机文件，也不需要 Server酱 Key

注意：平台会为避开早高峰排队而**提前触发**，首次下一次触发时间实际显示为 **2026-11-19 07:05** 左右，不是整点 08:00。这是平台行为，不是配置错误。

## 本机守护进程

`aqi_daemon.py` 每 30 分钟检查一次，只在污染时发微信。因为日报已交给平台任务，本机 `config.json` 里 `daily_report` 已置为 `false`，不会再重复发日报。

| 文件 | 作用 |
|---|---|
| `aqi_monitor.py` | 单次检查：拉数据 → 算 AQI → 判断 → 推送 |
| `aqi_daemon.py` | 常驻循环，带单实例锁 |
| `start_daemon.sh` / `stop_daemon.sh` | 后台启停 |
| `config.json` | 坐标、阈值、推送 Key、检查间隔 |
| `aqi_monitor.log` | 每次检查结果 |
| `daemon.log` | 守护进程心跳 |
| `aqi_state.json` | 污染告警状态 |

## 常用命令

```bash
cd /home/user/Doubao/chats/240571508249090/aqi-monitor

./start_daemon.sh                  # 启动（容器重启后需要手动跑一次）
./stop_daemon.sh                   # 停止
ps -p "$(cat daemon.pid)"          # 确认活着
tail -f daemon.log                 # 心跳
tail -f aqi_monitor.log            # 检查结果
python3 aqi_monitor.py --dry-run   # 看一次数据，不推送
python3 aqi_monitor.py --test      # 发一条微信测试
```

## 配置项

```json
{
  "city": "上海",
  "latitude": 31.2304,
  "longitude": 121.4737,
  "push_type": "auto",
  "send_key": "SCT...",
  "alert_aqi": 100,
  "recover_notify": true,
  "cooldown_minutes": 180,
  "state_file": "aqi_state.json",
  "interval_minutes": 30,
  "daily_report": false,
  "daily_report_time": "08:00"
}
```

改完不用重启，下一轮自动生效。

| 想做的事 | 改哪个 |
|---|---|
| 污染阈值改成 75（良就提醒） | `alert_aqi: 75` |
| 污染期间每小时催一次 | `cooldown_minutes: 60` |
| 转好不通知 | `recover_notify: false` |
| 检查频率改 15 分钟 | `interval_minutes: 15` |
| 恢复本机发日报（与平台任务会重复） | `daily_report: true` |

## 数据源与精度

`Open-Meteo Air Quality API`，免注册免 Key，六项污染物浓度 + 逐小时预报。

AQI 由 HJ 633-2012 分段线性公式自己算，不是直接吞第三方 AQI，等级口径与国内官方一致。

它是 **CAMS 全球模型的分析场**，不是上海国控站实测小时值，**通常偏差 10～30%**，边界情况可能差一档。判断"要不要提醒戴口罩"够用，别当官方数据引用。

要站点级精度就换和风天气或上海公共数据平台，改 `fetch_air()` 一个函数即可。

## 已知限制

- 本机守护进程容器重启后不会自动拉起（无 init 系统、平台配置目录不可写）。发现进程没了，跑一次 `./start_daemon.sh`。
- 日报由平台任务负责，不受本机重启影响，但平台可能提前或延后触发。
- Server酱免费版每天 5 条额度，本机只发污染预警，够用。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 正常 |
| 1 | 配置错误 / 重复启动被拒 |
| 2 | 数据获取失败 |
| 3 | 推送失败（Key 无效或额度用完） |
