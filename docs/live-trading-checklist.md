# 实盘接入 Checklist

> 当前状态：`DRY_RUN_ONLY`。本清单全部完成前，禁止真实下单。

## 1. P0-A：隔离边界

- [ ] Core 与 executiond 使用不同 OS 用户。
- [ ] Core store 与 execution store 使用不同路径。
- [ ] Core 用户不能读取或写入 execution store。
- [ ] executiond 对 Core store 只有读取计划快照所需的权限。
- [ ] 行情凭证与交易凭证已分离，交易凭证只注入 executiond。
- [ ] `tradingcat-executiond.service` 的 systemd 沙箱参数已在目标机器验证。
- [ ] Core 不能调用真实 Broker RPC；executiond 不接受任意订单字段。
- [ ] 运行以下命令并记录 P0-A：

```bash
./.venv/bin/python scripts/acceptance_v5.py \
  --record-p0a --confirm-deployment-isolation
```

该命令不创建 Live Canary，也不提交订单。

## 2. 自动验收

- [ ] `python -m pip check` 通过。
- [ ] `python -m pytest -q` 全部通过。
- [ ] `python e2e_full.py` 的 12 个阶段通过。
- [ ] `python scripts/acceptance_v5.py` 返回 `automated_acceptance=PASS`。
- [ ] SDK 诊断显示版本 4.4.3、只读行情 PASS。
- [ ] DRY_RUN 计划、审批、Intent、部分成交、成交和对账状态机演练通过。
- [ ] 计划篡改、ApprovalProof 重放/过期、账户非 SYNCED、订单 UNKNOWN 都被拒绝。

## 3. P0-B：用户对极小额验证的单独授权

用户必须明确给出且记录：

- [ ] 账户 ID；
- [ ] 标的与允许方向；
- [ ] 最大单笔和累计名义金额；
- [ ] 最大订单数；
- [ ] Canary 开始和失效时间；
- [ ] 允许的订单类型与最大滑点；
- [ ] 异常时的人工联系人和停止条件。

没有这些字段，不得根据聊天上下文推断授权。

## 4. 创建 Live Canary

- [ ] Canary 只能由人工运维动作创建，不能由 Agent 或计划生成逻辑创建。
- [ ] Canary 绑定 P0-A readiness 记录和用户授权范围。
- [ ] Canary 默认只允许一次极小额订单。
- [ ] Canary 超时、达到金额/次数限制或出现 UNKNOWN 后自动关闭。
- [ ] 普通 LIVE 路径继续保持关闭。

## 5. 逐笔审批与提交

- [ ] Core 生成不可变 ExecutionPlan，展示 `plan_id`、`plan_hash`、symbol、side、qty、
      order type、reference price、slippage 和 expires_at。
- [ ] 用户通过受信审批通道对准确的 `plan_hash` 提交 ApprovalProof。
- [ ] executiond 验证身份、owner 映射、nonce、时效、plan hash 和 Canary 范围。
- [ ] executiond 的 `health`、`readiness`、`execute_status`、`reconcile_status` 和
      `reconcile` RPC 仅接受标识符；它们不能携带或覆盖订单字段。LIVE `reconcile`
      必须显式启用只读订单查询；PAPER 对账仅检查本地 execution store。
- [ ] executiond 重新同步账户和行情。
- [ ] PreTradeRisk 只能 PASS/REJECT，不能缩量或修改订单。
- [ ] PASS 后由 OrderManager 原子消费 Confirmation 并创建幂等 Intent。
- [ ] LiveBroker 返回真实 broker_order_id 后，人工在长桥 App 中核对。
- [ ] 处理 submitted / partial fill / filled / cancelled / rejected 全部事件。
- [ ] Reconciliation 通过后关闭 Canary。

## 6. 立即停止条件

任一情况出现时，立刻关闭 Canary，禁止下一单：

- 没有 BrokerAck 或 broker_order_id 不一致；
- 本地与券商的方向、数量、价格或状态不同；
- OrderIntent 进入 UNKNOWN；
- AccountState 不是 SYNCED；
- Reconciliation 返回 MISMATCH；
- ApprovalProof 重放、过期或身份不匹配；
- 订单超过 Canary 金额、次数或有效期；
- 用户撤回授权。

## 7. 代码审计

```bash
# 真实提交私有入口只允许在 broker_live.py 内被调用
grep -RIn "_submit_live" execution --include='*.py'

# Broker 提交只允许来自 OrderManager 安全链
grep -RIn "submit_order(" . --include='*.py' --exclude-dir=.venv

# 自动调度不得出现 LIVE 或 Canary 创建命令
grep -RInE "LIVE|canary" deploy --include='*.service' --include='*.sh' \
  --include='*.example'
```

发现任何绕过 executiond、Confirmation、ApprovalProof 或 PreTradeRisk 的路径，视为阻断
级缺陷，必须先恢复 `DRY_RUN_ONLY` 再处理。
