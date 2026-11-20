# 空气质量监测 + 微信提醒

一个自包含的小工具：定时拉取空气质量数据，自己按国标算出 AQI，超过阈值时推送到微信；空气质量转好时可选发一条恢复通知。全程不需要注册第三方 AQI 服务，只依赖一个 Server酱（或 PushPlus）的推送 Key。

## 代码结构

| 文件 | 作用 |
|---|---|
| `aqi_monitor.py` | 单次检查：拉数据 → 算 AQI → 判断是否该推 → 推送，一次执行即退出 |
| `aqi_daemon.py` | 常驻循环，按 `interval_minutes` 反复调用检查逻辑，带单实例锁（`daemon.lock`） |
| `start_daemon.sh` / `stop_daemon.sh` | 后台启停守护进程（`setsid nohup`，脱离终端） |
| `install_cron.sh` | 另一种部署方式：往系统 crontab 写一条定时任务，替代常驻进程 |
| `config.example.json` | 配置模板，复制成 `config.json` 后填 Key |
| `aqi_state.json` | 运行状态：上次等级、上次检查时间、上次推送时间（用于去重与冷却） |
| `aqi_monitor.log` | 每次检查一行结果 |
| `daemon.log` | 守护进程心跳与启停记录 |

`aqi_monitor.py` 同时支持三种调用方式：正常运行（按配置决定推不推）、`--dry-run`（只取数打印，不推送）、`--test`（发一条测试消息验证 Key 是否可用）。

## 快速开始

```bash
cd aqi-monitor
cp config.example.json config.json     # 然后把 send_key 换成自己的
python3 aqi_monitor.py --dry-run       # 先确认能取到数、AQI 算得对
python3 aqi_monitor.py --test          # 确认微信能收到
./start_daemon.sh                      # 常驻后台，每 interval_minutes 检查一次
```

不想用常驻进程，也可以改用系统定时任务：

```bash
./install_cron.sh
```

## 常用命令

```bash
./start_daemon.sh                  # 启动（重启机器后需要手动跑一次）
./stop_daemon.sh                   # 停止并清理 pid
ps -p "$(cat daemon.pid)"          # 确认进程还活着
tail -f daemon.log                 # 心跳
tail -f aqi_monitor.log            # 每次检查结果
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

改完不用重启守护进程，下一轮检查自动读到新值。

| 想做的事 | 改哪个 |
|---|---|
| 阈值改成 75（良就提醒） | `alert_aqi: 75` |
| 污染期间每小时催一次 | `cooldown_minutes: 60` |
| 空气质量转好时不通知 | `recover_notify: false` |
| 检查频率改 15 分钟 | `interval_minutes: 15` |
| 开启每日一条日报 | `daily_report: true`，时间由 `daily_report_time` 决定 |
| 换城市 | 同时改 `city`、`latitude`、`longitude` |

`push_type` 为 `auto` 时，按键名形状自动识别 Server酱（`SCT...`）还是 PushPlus，不用手动指定。

**`config.json` 里是真实推送 Key，不要提交到版本库**——仓库已用 `.gitignore` 排除，模板只保留 `config.example.json`。

## 数据源与精度

`Open-Meteo Air Quality API`，免注册免 Key，返回六项污染物浓度 + 逐小时预报。

AQI 由代码按 HJ 633-2012 分段线性公式自己算（取六项分指数最大值，并给出首要污染物），等级口径与国内官方一致，不是直接吞第三方 AQI。

它背后是 **CAMS 全球模型的分析场**，不是某个城市国控站的实测小时值，**通常偏差 10～30%**，边界情况可能差一档。用来判断"要不要提醒戴口罩"足够，别当官方数据引用。

想要站点级精度就换和风天气或当地环保部门的监测数据接口，只改 `fetch_air()` 一个函数即可，计算逻辑不用动。

## 已知限制

- 守护进程不依赖 init 系统，异常退出或机器重启后不会自动拉起，需要手动 `./start_daemon.sh`，或者改用 `install_cron.sh` 交给系统定时任务
- Server酱免费版每天 5 条额度，默认配置只在污染超标时推送，够用
- 数据源精度受模型限制，见上一节

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 正常 |
| 1 | 配置错误 / 重复启动被拒 |
| 2 | 数据获取失败 |
| 3 | 推送失败（Key 无效或额度用完） |
