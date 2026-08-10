#!/usr/bin/env bash
# TradingCat 调度分发器 — 由 tc-schedule.timer 触发，按当前 HH:MM 分发任务。
# 时刻对应关系（北京时间 Asia/Shanghai，对齐现有开发环境 cron 调度）：
#   20:00 / 20:20 → 盘前 pre    (--scope portfolio / watchlist)
#   23:00 / 23:20 → 盘中 intra  (--scope portfolio / watchlist)
#   06:00 / 06:20 → 盘后 post   (--scope portfolio / watchlist)
#   17:35         → 订阅报告     (subscribe run)
#   19:40 / 06:30 → 账户+持仓同步 (account sync)
#   19:45 / 06:35 → 当前组合风控   (risk check)
#   19:50 / 06:40 → 活跃订单对账   (execution reconcile)
#   06:50         → WAL-safe 日备份 (backup daily)
#   周日 07:10    → WAL-safe 周归档 (backup weekly)
# 其它时刻触发则跳过（timer 已按星期限定，脚本只需按时刻分发）。
# 日志：${TS_ROOT}/deploy/logs/dispatcher.log（逐任务输出同样追加其中）
set -euo pipefail

TS_ROOT="${TS_ROOT:-/opt/tradingcat/skills/trading-system}"
export PYTHONPATH="${TS_ROOT}"
# 长桥 OpenAPI token（若未通过 tc-scheduler.service 的 Environment 注入，
# 可在此 export；变量名与代码实际读取一致）：
# export LONGBRIDGE_APP_KEY=your_app_key
# export LONGBRIDGE_APP_SECRET=your_app_secret
# export LONGBRIDGE_ACCESS_TOKEN=your_access_token
# 可选：自定义 DB 路径（缺省 ${TS_ROOT}/shared/trading.db）：
# export TRADING_DB=/path/to/trading.db
# 可选：订阅报告推送 webhook（推荐方式下订阅推送需要；未配置则只落盘）：
# export TRADINGCAT_WEBHOOK_URL=https://your.webhook.example/hook
# Python SDK 可选区域覆盖；通常留空，亚太账户且大陆网络需要时可设 cn：
mkdir -p "${TS_ROOT}/deploy/logs"
cd "${TS_ROOT}"

slot="$(date +%H%M)"
LOG="${TS_ROOT}/deploy/logs/dispatcher.log"

run() {
  echo "[$(date '+%F %T %Z')] >>> python3 tc.py $*" >> "${LOG}"
  python3 "${TS_ROOT}/tc.py" "$@" >> "${LOG}" 2>&1
}

case "${slot}" in
  1940|0630) run account sync ;;
  1945|0635) run risk check ;;
  1950|0640) run execution reconcile ;;
  0650) run backup daily ;;
  0710)
    if [[ "$(date +%u)" == "7" ]]; then run backup weekly; fi
    ;;
  2000) run monitor pre --scope portfolio ;;
  2020) run monitor pre --scope watchlist ;;
  2300) run monitor intra --scope portfolio ;;
  2320) run monitor intra --scope watchlist ;;
  0600) run monitor post --scope portfolio ;;
  0620) run monitor post --scope watchlist ;;
  1735) run subscribe run ;;
  *)    echo "[$(date '+%F %T %Z')] no task at ${slot}, skip" >> "${LOG}" ;;
esac
