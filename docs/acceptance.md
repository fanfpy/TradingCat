# TradingCat 验收记录

> 历史验收日期：2026-08-10
> 架构基线：[architecture.md](architecture.md)  
> 自动验收证据：`75828672565c14cb1edadaba4bf179445e7be7a431f5895724fd8f892a8c663b`

## 结论

Agent 无关契约、研究可信性、个人投资者闭环和执行安全链已经通过自动验收。当前状态
仍是 `DRY_RUN_ONLY`：目标机器的独立 OS 用户/数据库权限尚未人工确认，P0-B 真实极小额
订单也未获用户明确授权。

## 自动验收结果

| 检查 | 结果 | 证据 |
|---|---|---|
| 全量测试 | PASS | 历史记录：164 passed；2026-08-13 复验：263 passed |
| DRY_RUN 全链路 | PASS | 12/12 phases |
| Application Contracts | PASS | Analyze/Follow/Review/Propose/Explain/RequestApproval |
| Agent 等价响应 | PASS | 不同 Agent 输入得到结构等价的 `tradingcat.v1` 响应 |
| 研究验证 | PASS | 嵌套 WF、Final Holdout、候选冻结、稳健性与成本门 |
| 因子/PIT | PASS | 当前数据与历史 PIT 契约隔离，未来数据不能回填历史 |
| 组合与仓位 | PASS | 收缩 Kelly、目标权重区间、动态成本和组合风险 |
| Signal + Outbox | PASS | 同事务、幂等、回滚和失败重试 |
| Core / executiond 边界 | PASS | 伪造审批、计划篡改、Proof 重放/过期全部拒绝 |
| Live Canary 规则 | PASS | 范围、金额、次数、时效和 UNKNOWN 自动关闭 |
| Longbridge SDK | PASS | 固定 4.4.3，API Key，AAPL 只读行情连通 |
| 当前基本面 | SAFE DEGRADE | SDK 能力按运行时探测；未配置合格 PIT Provider 时明确缺失 |
| 历史基本面 | SAFE DEGRADE | 没有合格 PIT Provider，不生成或回填历史基本面因子 |
| Longbridge Quant | OPTIONAL | 当前 CLI 无 quant；Native 最终验证不受影响 |
| QwenPaw | UNCHANGED | 仍为独立 Agent；TradingCat 不修改其镜像或 Compose |
| 真实订单 | NOT RUN | 必须由用户另行明确授权 P0-B |

## AAPL 真实只读闭环

- 行情：1000 根真实日线，官方交易日历，实时价 313.33；
- 分析：技术因子可用，当前与历史基本面均为 `MISSING_SAFE_DEGRADE`；
- 研究：`degraded / score=41.61 / structure_not_robust`，正确阻止生成真实证据交易计划；
- 关注：加入关注清单并检测到盘前入场区域提醒；
- 安全：TradeContext 创建 0 次、真实账户读取 0 次、真实下单 0 次；
- 证据：`46e72081d8b3234a1b81abdeaadb405848ce1e953b583497b2dba09482b5e89d`。

机器可读记录位于 `reports/acceptance-personal-loop.json`。

## 开源发布验收

在主动移除全部 Longbridge 环境变量、跳过外部网络连接的条件下完成：

| 检查 | 结果 |
|---|---|
| Apache-2.0 `LICENSE` / `NOTICE` | PASS |
| 当前发布候选敏感信息检查 | PASS（不输出疑似值） |
| Git 历史敏感信息检查 | PASS（未发现已知凭证模式） |
| 无凭证全量测试 | PASS（164 passed） |
| 无凭证 DRY_RUN 全链路 | PASS（12/12 phases） |
| 离线 SDK 版本与能力检查 | PASS（longbridge 4.4.3） |
| wheel / sdist 构建 | PASS |
| wheel 独立安装及两个 CLI 入口 | PASS |
| 制品内凭证、数据库和运行数据检查 | PASS |
| 真实订单 | NOT RUN |

离线自动验收证据：
`bf6deb02eb378f99d767cdfb90d5767e81f4a258064e6c17aba08eb6f132702a`。
发布前仍须在导出的独立仓库中运行 Gitleaks，以覆盖该仓库最终 Git 历史。

## 复验

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python e2e_full.py
./.venv/bin/python scripts/acceptance_personal_loop.py
./.venv/bin/python scripts/acceptance_v5.py --no-connect
./.venv/bin/python scripts/check_open_source.py
```

以上命令不会创建 Live Canary 或提交真实订单。

## 2026-08-13 离线复验

本次在 Windows worktree 中使用隔离的非存在 `TRADINGCAT_ENV_FILE`，未连接 Longbridge、
未读取真实凭证、未创建 Canary、未提交订单：

| 检查 | 结果 | 证据 |
|---|---|---|
| `pip check` | PASS | `No broken requirements found` |
| 全量 pytest | PASS | `263 passed` |
| `e2e_full.py` | PASS | 12/12 phases |
| `acceptance_v5.py --no-connect` | PASS | `automated_acceptance=PASS` |
| `check_open_source.py` | PASS | `files_scanned=146` |
| Linux/systemd P0-A | NOT_RUN | 当前环境为 Windows |
| P0-B 真实订单 | NOT_RUN | 需要用户明确授权；本次未连接券商 |

验收脚本在 Windows 非 UTF-8 locale 下的子进程输出读取问题已修复为 UTF-8 且非法字节
安全替换，避免丢失验收证据。当前系统仍为 `DRY_RUN_ONLY`。

## 仍需人工完成

1. 在目标机器用独立 `tradingcat-exec` OS 用户部署 executiond；
2. 验证 Core/Execution 两个 store 的最小权限隔离；
3. 运行带 `--record-p0a --confirm-deployment-isolation` 的自动验收记录 P0-A；
4. 若确需真实链路测试，用户必须另行定义 P0-B 的账户、标的、方向、金额、次数和时效，
   并逐笔提供 ApprovalProof。

生产接入步骤以 [live-trading-checklist.md](live-trading-checklist.md) 为准。
