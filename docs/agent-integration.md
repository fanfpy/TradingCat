# TradingCat Agent 集成手册

本文是 QwenPaw、Codex、Trae、MCP/HTTP adapter 或其他 Agent 调用 TradingCat 的操作
契约。Agent 应以本文件和 `SKILL.md` 为准，不解析 README 中的自然语言来猜命令。

## 1. 调用原则

1. 把包含 `SKILL.md` 的目录作为 `TS_ROOT`，不要硬编码 QwenPaw 或当前服务器路径。
2. 优先通过 `application.cli` 的 JSON stdin/stdout 契约调用业务用例。稳定短操作名为
   `analyze`、`backtest`、`propose`、`paper`、`status`、`report`、`request-approval`、
   `approve` 和 `execute`；旧的带资源名操作仍兼容。
3. 只有研究缓存、监控、账户同步、备份等运维动作才调用 `./tc`。
4. 以退出码和 JSON envelope 共同判断结果；始终向用户展示 `warnings`。
5. Agent 可以分析、关注、复核、提出计划和申请审批；不能制造 `ApprovalProof`。在可信
   人工审批已完成后，Agent 可以调用 `execute` 转发不可变的 `plan_id` 与 `confirmation_id`。

推荐进程调用方式：

```text
argv = ["<TS_ROOT>/.venv/bin/python", "-m", "application.cli", operation]
cwd = <TS_ROOT>
stdin = JSON.stringify(payload)
shell = false
```

若项目虚拟环境不存在，先报告安装未完成，不要自行改用系统中不确定版本的 Longbridge
SDK。正式支持版本固定为 `longbridge==4.4.3`。

## 2. 稳定入口

所有入口读取一个 JSON object，并在 stdout 输出一个 JSON envelope。退出码为：`0` 成功，
`1` 业务/安全门失败，`2` 参数、JSON 或未知操作失败。

| 操作 | 作用 | 安全边界 |
|---|---|---|
| `analyze` | 标的分析 | 只读分析 |
| `backtest` | 本地 bars 回测 | 只读研究，不授予交易资格 |
| `propose` | 组合建议与计划 | 默认 `PAPER`，不触达券商 |
| `paper` | 本地纸面建议 | 强制 `PAPER`，不触达券商 |
| `status` | 账户/计划/安全状态 | 只读，明确 `live_enabled=false` |
| `report` | 本地状态摘要 | 只读、`LOCAL_ONLY` |
| `request-approval` | 创建待审批 Confirmation | 只创建 `PENDING`，必须绑定 `plan_hash` |
| `approve` | 记录可信人工审批 | 只接受完整、签名的 `ApprovalProof`；Agent 不得生成 |
| `execute` | 执行已批准计划 | 只接受 `plan_id`、`confirmation_id`；由 executiond 执行 |

`propose` 收到 `mode=LIVE` 只有在账户为 `SYNCED` 且存在非空可执行订单时才会生成不可变计划
并返回 `PENDING_APPROVAL`；不会批准或提交。没有可执行订单时返回 `NO_ACTION` 或
`BLOCKED`，`execution_plan=null`、`requires_explicit_human_approval=false`、
`approval_status=null`，且不会持久化空 LIVE 计划。`paper` 始终强制 `PAPER`。LIVE 的批准
必须来自可信人工审批通道，执行必须经过 executiond。

## 3. 意图分发表

| 用户意图 | Agent 操作 | 接口 | 是否写数据 | 后续动作 |
|---|---|---|---|---|
| “分析苹果怎么样” | 分析股票 | `analyze-security` | 可能初始化标的主数据 | 展示因子、策略状态、warnings、lineage |
| “研究/回测 AAPL” | 建候选并验证 | `tc research ...` | 是 | 完成后再次 `analyze-security` |
| “关注苹果” | 加入关注 | `follow-security` | 是 | 说明关注不等于交易资格 |
| “有没有买卖信号” | 运行监控 | `tc monitor pre/intra/post` | 是 | 展示信号，不创建订单 |
| “看看我的持仓” | 组合复核 | `review-portfolio` | 否 | 展示 KEEP/ADD/REDUCE/EXIT 与风险标志 |
| “建议买多少” | 生成建议计划 | `propose-trade` | 是 | 展示权重、plan_hash，等待用户决策 |
| “申请审批这个计划” | 创建待审批请求 | `request-approval` | 是 | 结果只能是 PENDING |
| “为什么建议这笔交易” | 解释证据 | `explain-decision` | 否 | 引用策略、数据、政策和审计 ID |
| “直接帮我买” | 不直接跳过审批 | 先 `propose-trade`；明确要求 LIVE 且返回非空计划时再走审批链 | 仅非空计划写入 | 展示计划和 hash；仅确有订单时保持 PENDING，等待可信人工审批 |

用户只提出分析、解释或 review 时，不要扩大为关注、同步账户、创建计划或申请审批。

## 4. JSON Operations

所有响应使用 `tradingcat.v1` envelope：

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

### 3.1 analyze-security

用途：解析标的并返回技术因子、当前基本面、PIT 因子、策略适用性和研究证据。

```json
{"query":"苹果"}
```

历史时点分析：

```json
{"query":"AAPL","as_of":"2025-12-31"}
```

调用：

```bash
printf '%s' '{"query":"苹果"}' |
  ./.venv/bin/python -m application.cli analyze-security
```

判读规则：

- `ok=false`：展示 `error`，不要猜测股票代码。
- `technical_factors=null`：数据不足，先征得用户同意后运行研究缓存流程。
- `research_status` 不是 `verified/live`：可以分析，但不能生成新入场结论。
- `trade_eligible=false`：明确说明尚不具备交易研究资格。
- 基本面缺失：保留 `MISSING_SAFE_DEGRADE`，不能当作 0，也不能回填历史。

### 3.2 follow-security

```json
{
  "query":"AAPL",
  "account_id":"default",
  "reason":"等待趋势策略入场信号",
  "channels":["audit","daily"]
}
```

```bash
printf '%s' '{"query":"AAPL","reason":"等待趋势策略入场信号"}' |
  ./.venv/bin/python -m application.cli follow-security
```

成功后必须告诉用户：关注已建立，但 `strategy_assignment` 仍可能为空，关注本身不授予
`trade_eligible`。

### 3.3 review-portfolio

```json
{"account_id":"default"}
```

```bash
printf '%s' '{"account_id":"default"}' |
  ./.venv/bin/python -m application.cli review-portfolio
```

逐持仓展示 `action`、`current_weight`、`target_weight_range`、`weight_delta`、
`stop_price`、`rationale` 和 `risk_flags`。若账户不是 SYNCED，建议只能用于诊断，LIVE
必须保持关闭。

### 3.4 propose-trade

```json
{
  "equity":100000,
  "account_id":"default",
  "mode":"DRY_RUN"
}
```

```bash
printf '%s' '{"equity":100000,"account_id":"default","mode":"DRY_RUN"}' |
  ./.venv/bin/python -m application.cli propose-trade
```

判读规则：

- `target_portfolio.passed=false`：新买入目标已经归零，不得自行缩量后重试。
- `execution_plan=null`：没有可审批计划。
- LIVE 空结果的 `data.status` 为 `NO_ACTION` 或 `BLOCKED`；使用 `data.error.code`、
  `data.details` 和 `warnings` 区分无信号、研究不合格、账户非 `SYNCED`、风控拒绝或
  没有可执行订单。
- 有计划：展示完整订单、`plan_id`、`plan_hash`、失效时间和 lineage。
- `requires_explicit_human_approval=true` 只表示需要审批，不表示已经批准。
- Agent 默认只使用 `PAPER`；不能根据用户含糊表述切换为 LIVE。

### 3.5 request-approval

```json
{"plan_id":"plan_xxx","plan_hash":"hash_xxx","idempotency_key":"approval-001"}
```

```bash
printf '%s' '{"plan_id":"plan_xxx"}' |
  ./.venv/bin/python -m application.cli request-approval
```

该接口只创建 `PENDING` Confirmation。Agent 不得制造 ApprovalProof、修改数据库状态或
把 PENDING 描述为已批准。executiond 不可用时返回 `EXECUTIOND_UNAVAILABLE`。

### 3.6 execute

```json
{"plan_id":"plan_xxx","confirmation_id":"confirmation_xxx"}
```

```bash
printf '%s' '{"plan_id":"plan_xxx","confirmation_id":"confirmation_xxx"}' |
  ./.venv/bin/python -m application.cli execute
```

`execute` 的输入只允许这两个不可变标识符；application 只会通过本机
`executiond` socket 调用，不会直接访问 broker。`EXECUTIOND_UNAVAILABLE` 表示可重试的
服务边界故障。`UNKNOWN_OUTCOME` 和重复执行的拒绝/状态均由 executiond 原样返回；不得
自动重试或推断成交结果。

### 3.7 explain-decision

```json
{"plan_id":"plan_xxx"}
```

```bash
printf '%s' '{"plan_id":"plan_xxx"}' |
  ./.venv/bin/python -m application.cli explain-decision
```

解释时引用响应中的 `strategy_version_ids`、`investor_policy_version_ids` 和 audit，不要
根据最新行情重新构造一个不同计划。

## 4. 研究与回测编排

当用户明确要求研究、回测，或同意补齐分析数据时，依次执行：

```bash
./tc research add AAPL.US
./tc research cache AAPL.US
./tc research prefilter AAPL.US
./tc research run AAPL.US --grid small
```

完成后再次调用 `analyze-security`，不要仅凭 `research run` 的终端摘要回答。

网格选择：

- 默认 `small` 用于交互式快速验证；
- 用户明确要求完整研究时使用 `full`；
- 需要测试 ADX 条件时使用 `adx`；
- 大规模或多标的完整回测前先说明耗时和数据需求。

研究结论必须区分：

- `verified`：研究证据通过，可以进入关注与仓位评估；
- `degraded`：证据不稳健，不生成新入场计划；
- `suspended/removed`：禁止新入场，但已有持仓仍需监控退出；
- `UNRESEARCHED`：只有描述性分析，没有策略资格。

## 5. 监控编排

```bash
./tc monitor pre --scope watchlist
./tc monitor intra --scope watchlist
./tc monitor post --scope watchlist
```

- `pre`：入场区域、当前止损、保护单和组合风险；
- `intra`：距离入场或止损边界较近的临界提醒；
- `post`：用完成日线确认入场、退出和止损上移。

Agent 应区分“接近边界”“正式信号”“策略可交易”和“用户已授权”，不能把前一状态
自动升级为后一状态。

## 6. 当前基本面

```bash
./tc market fundamentals AAPL.US --json
```

未配置 `TRADINGCAT_OPENALICE_ADAPTER_COMMAND` 时，命令返回空 snapshots、明确 warning
和退出码 2。这是可选能力缺失，不是行情系统故障。Agent 应继续技术面分析并清楚说明
局限，不调用 Longbridge CLI/OAuth。

外部 Provider 的 current 数据只回答公司现状，不具备历史 PIT 资格。

## 7. 退出码与失败处理

| 退出码 | Agent 行为 |
|---|---|
| 0 | 继续解析 JSON；仍需展示 warnings |
| 1 | 停止当前链路，展示 error，不伪造结果 |
| 2 | 检查是否为参数错误；基本面无 Provider 时按安全降级解释 |

JSON CLI 若 `ok=false`，即使外层工具没有完整传递退出码，也必须视为失败。不要吞掉
stderr，不要从部分 stdout 猜结果。

## 8. 实盘红线

Agent 永远不得：

- 直接调用 `LiveBroker`、`_submit_live` 或 Longbridge 下单 API；
- 将 `followed`、`verified`、`ExecutionPlan` 或 `PENDING` 当作用户批准；
- 生成、复制或重放 ApprovalProof；
- 修改已经批准计划的 symbol、side、quantity、price、mode 或有效期；
- 在账户 STALE/UNKNOWN、订单 UNKNOWN、对账 MISMATCH 时继续提交；
- 创建 Live Canary 或在自动验收中运行真实订单。

若用户明确要求真实交易，先读取 `docs/live-trading-checklist.md`，确认 P0-A/P0-B 状态。
没有生产部署验收、账户同步、可信人工审批和 Canary 范围时，不能调用 `execute`；若账户
未 `SYNCED` 或没有非空订单，提案应保持 `BLOCKED`/`NO_ACTION`，不能创建空
`PENDING_APPROVAL` 计划。

## 9. Agent 对用户的标准回答结构

完成操作后按以下顺序回答：

1. **结论**：分析、关注、持仓建议或计划是否成功。
2. **证据**：关键因子、研究状态、目标权重、计划 hash 或 lineage。
3. **风险与缺失**：完整展示 warnings、数据时点和降级能力。
4. **安全状态**：是否仅分析、DRY_RUN、PENDING，明确没有真实成交。
5. **下一步**：只建议一个与当前状态相符的可执行动作。

不要使用“系统建议买入”这种省略证据状态的表述。应写成例如：“策略研究状态为
verified，组合风控通过，系统生成 3%–4% 的 DRY_RUN 目标区间；计划尚未获得人工审批，
没有下单。”
