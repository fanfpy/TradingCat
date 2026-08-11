# Contributing to TradingCat

感谢你参与 TradingCat。项目优先保证研究可复现、数据时点正确和实盘安全边界，功能便利性
不能绕过这些约束。

## 开始之前

1. 阅读 `README.md`、`docs/architecture.md` 和 `docs/agent-integration.md`。
2. 涉及真实资金、审批或券商提交时，额外阅读 `docs/live-trading-checklist.md`。
3. 对较大功能先创建 Issue，说明问题、设计边界、数据来源和验收方法。
4. 不要在 Issue、日志、测试或提交中包含真实密钥、账户号、订单号或个人持仓。

## 本地开发

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env  # 只有需要真实只读行情时才填写；不要提交
```

公共测试不需要任何券商凭证：

```bash
TRADINGCAT_ENV_FILE=/tmp/tradingcat-no-env \
  ./.venv/bin/python -m pytest -q
TRADINGCAT_ENV_FILE=/tmp/tradingcat-no-env \
  ./.venv/bin/python e2e_full.py
./.venv/bin/python scripts/check_open_source.py
```

## 修改要求

- 保持 `longbridge==4.4.3`；升级 SDK 必须单独提案并提供跨平台证据。
- 业务数据库访问必须经过 `shared/db.py`，不要直接打开 SQLite。
- 历史基本面必须提供 period/published/available/source，禁止 current 数据回填 PIT。
- 回测最终资格只能由 Native 引擎、嵌套 Walk-Forward 和 Final Holdout 决定。
- 不得让 Agent、监控、调度或普通 CLI 生成 LIVE `APPROVED`。
- PreTradeRisk 只能 PASS/REJECT，不能修改已批准订单。
- 测试不得创建真实 TradeContext 或调用真实下单接口。
- 新增 Agent 能力时同步更新 `SKILL.md` 和 `docs/agent-integration.md`。

## Pull Request

PR 请包含：

- 问题与设计说明；
- 影响的安全/数据边界；
- 新增或更新的测试；
- 实际运行的验证命令；
- 是否改变数据库 schema、Agent contract 或 CLI；
- 明确声明没有提交密钥、真实账户数据和真实订单。

保持提交聚焦，不混入无关格式化或生成文件。所有贡献默认按 Apache License 2.0 提交，
除非贡献者明确书面标注为 “Not a Contribution”。
