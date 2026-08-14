# TradingCat 使用说明

本文面向个人投资者和需要接入 TradingCat 的 Agent。部署与 systemd/cron 配置见
[deploy.md](deploy.md)，架构和安全边界见 [architecture.md](architecture.md)。Agent
实现方应同时完整阅读 [agent-integration.md](agent-integration.md)。

## 0. 通过 Agent 第一次使用

先让 Agent 读取仓库根目录 `SKILL.md`，然后直接描述目标，不必先学习命令。例如：

- “TradingCat 怎么用？只读检查当前环境并告诉我一个下一步。”
- “分析 AAPL；缺数据时先解释，不要自动下载或回测。”
- “查看持仓风险；不要自动同步账户。”
- “用 PAPER 模式演练交易，禁止实盘。”

Agent 的第一次回应应包含：你的目标、当前能力与限制、当前安全模式，以及一个推荐的
下一步。除非你明确要求，否则分析和教学均为只读。

以下操作会改变本机、TradingCat 或外部系统状态，Agent 必须先获得明确授权：

| 操作 | 典型影响 |
|---|---|
| 安装依赖、创建或修改 `.env` | 改变本机环境或配置 |
| 缓存行情、运行研究 | 访问网络并写入数据/研究状态 |
| 添加关注或订阅 | 修改关注清单和调度范围 |
| 同步账户、执行对账 | 读取外部账户并写入账户/订单事实 |
| 生成持久化计划、请求审批 | 创建交易工作流状态 |
| 执行已批准计划 | 可能向隔离执行服务提交真实订单 |

Agent 应使用项目虚拟环境：Windows 为 `.venv\Scripts\python.exe`，Linux/macOS 为
`.venv/bin/python`。虚拟环境缺失时应停止并提供安装选项，不应静默安装或改用不确定的
系统 Python。

### 状态语言

| 状态 | 含义 |
|---|---|
| `仅分析` | 只读解释，没有改变系统状态 |
| `研究不足` | 数据或冻结研究证据不足 |
| `研究已验证` | 冻结策略证据通过研究门禁 |
| `PAPER` | 仅模拟 |
| `NO_ACTION` | 没有需要执行的订单 |
| `BLOCKED` | 安全门禁阻止继续 |
| `PENDING_APPROVAL` | 等待可信人工审批，并非已经批准 |
| `已批准但未执行` | 审批完成，但没有提交订单 |
| `已提交等待对账` | 已提交，但成交和对账尚未最终确认 |

四个容易混淆的边界：关注不等于交易资格；`verified` 不等于用户批准；
`PENDING_APPROVAL` 不等于已批准；`SUBMITTED` 不等于已成交。

## 1. 安装与检查

```bash
cd /path/to/trading-system
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

在 `.env` 中填入同一个长桥开放平台 Legacy API Key 应用的：

```dotenv
LONGBRIDGE_APP_KEY=...
LONGBRIDGE_APP_SECRET=...
LONGBRIDGE_ACCESS_TOKEN=...
```

运行只读诊断：

```bash
./.venv/bin/python shared/sdk_diagnostics.py --connect --symbol AAPL.US
```

期望结果：`version=4.4.3`、`connectivity=PASS`。未配置合格 PIT 数据源时，基本面显示
`UNSUPPORTED_SAFE_DEGRADE` 是正常状态，不代表行情异常。

## 2. 两种调用方式

### 人工与运维：`tc`

```bash
./tc --help
./tc research --help
./tc monitor --help
```

`tc` 输出适合终端阅读，部分命令支持 `--json`。

Windows PowerShell 使用等价入口：

```powershell
.\tc.ps1 --help
.\tc.ps1 market quote AAPL.US --json
```

Linux/macOS 使用 `./tc`。Windows wrapper 会优先使用项目虚拟环境，再检查可用的
`python`、`py -3` 和 `python3`；如果都不是可运行的 Python 3.10+，会明确提示安装
Python 或创建 `.venv\Scripts\python.exe`，不会模糊调用损坏的 launcher。

### Agent 与系统集成：JSON 契约

```bash
printf '{"query":"苹果"}' |
  ./.venv/bin/python -m application.cli analyze-security
```

JSON 契约比终端文本稳定，响应统一为：

```json
{
  "schema_version": "tradingcat.v1",
  "request_id": "req_...",
  "operation": "AnalyzeSecurity",
  "ok": true,
  "data": {},
  "error": null,
  "warnings": [],
  "lineage": {}
}
```

Agent 必须展示 `warnings`，不能把缺失数据解释为零，也不能把“关注”解释为“可以买入”。

稳定短入口是 `analyze`、`backtest`、`propose`、`paper`、`status`、`report`、
`request-approval`、`approve` 和 `execute`。`propose` 默认使用本地 PAPER 语义，`paper`
强制 PAPER；显式传入 `mode=LIVE` 时，只有账户为 `SYNCED` 且存在非空订单才生成
`PENDING_APPROVAL` 的不可变计划，不会批准或下单。无订单时返回 `NO_ACTION` 或 `BLOCKED`，
`execution_plan` 为 `null`，不会持久化空 LIVE 计划。

## 3. 分析一只股票

首次研究建议按顺序执行：

```bash
./tc research add AAPL.US
./tc research cache AAPL.US
./tc research prefilter AAPL.US
./tc research run AAPL.US --grid small
```

- `small`：快速验证工作流；
- `full`：完整参数网格，耗时更长；
- `adx`：完整网格并允许 ADX 过滤，由验证过程选择是否启用。

读取 Agent 友好的分析结果：

```bash
printf '{"query":"AAPL"}' |
  ./.venv/bin/python -m application.cli analyze-security
```

重点字段：

- `technical_factors`：趋势、动量、波动、RSI、ADX、流动性；
- `current_fundamentals`：当前基本面；未配置 Provider 时为空并附 warning；
- `strategy_suitability`：适合与不适合的策略环境；
- `research_status`：`verified` 才具备生成新入场建议的研究资格；
- `strategy_candidate`：冻结参数、稳健性和 Holdout 证据；
- `lineage`：数据版本与策略版本。

## 4. 关注与信号提醒

通过 JSON 契约关注：

```bash
printf '{"query":"AAPL","reason":"等待趋势策略验证"}' |
  ./.venv/bin/python -m application.cli follow-security
```

或通过日频订阅命令：

```bash
./tc subscribe add AAPL.US --push-daily
./tc subscribe list
./tc subscribe run --symbol AAPL.US
```

运行三态监控：

```bash
./tc monitor pre --scope watchlist
./tc monitor intra --scope watchlist
./tc monitor post --scope watchlist
```

- 盘前：入场区域、当前止损、保护单缺失与组合风险；
- 盘中：距离入场/止损边界较近时提醒，同日去重；
- 盘后：只用完成日线确认入场、退出和止损上移。

监控只产生信号和通知，不会自动下单。

## 5. 持仓管理与 Kelly 建议

需要最新真实账户事实时，在用户明确同意后执行同步：

```bash
./tc account sync
./tc account show
./tc position --symbol AAPL.US
./tc risk check
```

Agent 获取整个组合的建议：

```bash
printf '{"account_id":"default"}' |
  ./.venv/bin/python -m application.cli review-portfolio
```

每个持仓返回：

- `action`：KEEP / ADD / REDUCE / EXIT；
- `current_weight` 与 `target_weight_range`；
- `weight_delta`：目标与当前权重差；
- `stop_price`：当前保护阈值；
- `risk_flags`：证据不足、账户不同步等风险；
- `rationale`：建议原因。

Kelly 只是上限之一。止损风险、单标的上限、相关性、行业/币种敞口、Beta、购买力和
流动性可以进一步压低目标仓位。

## 6. 生成交易建议

人工命令：

```bash
./tc portfolio build --equity 100000 --mode DRY_RUN
```

Agent 契约：

```bash
printf '{"equity":100000,"account_id":"default","mode":"DRY_RUN"}' |
  ./.venv/bin/python -m application.cli propose-trade
```

返回的 `execution_plan` 是不可变建议，不是成交。`requires_explicit_human_approval=true`
表示后续必须由真实用户审批指定的 `plan_hash`。

LIVE Agent 流程：

```bash
# 生成待审批 LIVE 计划
printf '%s' '{"equity":100000,"account_id":"default","mode":"LIVE"}' |
  ./.venv/bin/python -m application.cli propose-trade

# 必须绑定返回的 plan_id 和 plan_hash
printf '%s' '{"plan_id":"plan_xxx","plan_hash":"hash_xxx","idempotency_key":"approval-001"}' |
  ./.venv/bin/python -m application.cli request-approval

# 受信人工审批通道完成 ApprovalProof 后，执行只传两个标识符
printf '%s' '{"plan_id":"plan_xxx","confirmation_id":"cfm_xxx"}' |
  ./.venv/bin/python -m application.cli execute
```

Agent 不得自行生成 `ApprovalProof`，也不得向 `execute` 传入 symbol、side、quantity、
price 或其他订单覆盖字段。

仅用于本地 PAPER 的人工演练：

```bash
./tc trade order --symbol AAPL.US --qty 1
```

该命令会展示完整计划并请求 y/N，但不会触达券商。LIVE CLI 即使输入确认短语也不能
生成 `APPROVED`；生产审批必须由隔离 executiond 验证 ApprovalProof。

## 7. 查询解释与审计血缘

```bash
printf '{"plan_id":"plan_xxx"}' |
  ./.venv/bin/python -m application.cli explain-decision
```

响应包含计划快照、策略版本、投资政策版本和审计事件。Agent 在解释建议时应引用这些
字段，不得凭上下文重新推导一个与原计划不同的订单。

## 8. 可选当前基本面

SDK 4.4.3 的基本面接口按能力探测；若配置 OpenAlice/TraderHub adapter：

```dotenv
TRADINGCAT_OPENALICE_ADAPTER_COMMAND=/usr/local/bin/openalice-tradingcat-adapter
```

TradingCat 会直接执行该 argv，不经过 shell，并向 stdin 写入：

```json
{
  "schema_version": "tradingcat.provider.v1",
  "operation": "current_fundamentals",
  "symbol": "AAPL.US"
}
```

adapter 应在 stdout 返回 JSON object，或返回 `{"data": {...}}`。这些数据只用于当前
公司分析，固定标记为非 PIT，不得用于历史回测。未配置时：

```bash
./tc market fundamentals AAPL.US --json
```

会返回无快照和明确 warning，退出码为 2；不会回退到 Longbridge CLI/OAuth。

## 9. 运维命令

```bash
./tc execution reconcile
./tc execution reconcile --plan-id PLAN_ID
./tc strategy list --symbol AAPL.US
./tc backup daily
./tc backup weekly
```

账户同步失败、券商状态未知或对账不一致时，命令返回非零并关闭新实盘路径。研究、监控
和报告仍可继续，但必须展示降级状态。

## 10. 常见退出码

| 退出码 | 含义 |
|---|---|
| 0 | 命令成功，或可选能力被明确检查为不可用但系统健康 |
| 1 | 查询、验证或安全门失败 |
| 2 | 参数错误，或可选基本面没有任何可用快照 |

自动化系统应同时检查退出码和 JSON 中的 `ok/warnings/error`。

## 11. 验收

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python e2e_full.py
./.venv/bin/python scripts/acceptance_personal_loop.py
./.venv/bin/python scripts/acceptance_v5.py
```

这些命令不会提交真实订单。真实资金接入必须另行完成
[live-trading-checklist.md](live-trading-checklist.md)。
