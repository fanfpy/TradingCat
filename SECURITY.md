# Security Policy

## Supported version

安全修复仅保证进入当前主分支。正式发布后，维护者会在此补充仍受支持的 release 分支。

## Reporting a vulnerability

请不要为以下问题创建公开 Issue：

- 凭证、Token、账户或持仓泄漏；
- 绕过 ApprovalProof、PreTradeRisk、Live Canary 或 executiond；
- 重放、幂等、订单篡改或越权提交问题；
- 可以触发真实下单或扩大订单范围的缺陷；
- 供应链或依赖接管风险。

仓库发布后，请通过 GitHub 的 **Private vulnerability reporting** 私密报告，并包含：

1. 受影响版本或 commit；
2. 最小复现步骤；
3. 可能的资金、凭证或数据影响；
4. 已知缓解措施；
5. 是否接触过真实账户或订单。

在修复发布前不要公开披露细节。维护者收到报告后应先确认问题、关闭相关 Live Canary，
必要时建议轮换凭证，再协调修复和披露时间。

## Secret handling

- 永远不要提交 `.env`、数据库、报告、备份、券商响应或真实订单数据。
- 泄漏的凭证必须立即在来源系统撤销并轮换；从 Git 最新提交删除并不等于从历史删除。
- QwenPaw、Codex、Trae 等 Agent 不应获得交易凭证；交易凭证只属于隔离 executiond。

## Safe default

TradingCat 默认 `DRY_RUN_ONLY`。发现可疑行为时，停止 executiond、关闭 Canary、撤销交易
凭证并执行券商/本地对账，研究和监控可以在只读模式继续。
