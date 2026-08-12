---
name: trading-system
description: Agent-independent personal quantitative investing workflow for stock analysis, strategy research and backtesting, watchlists and signal monitoring, Kelly position sizing, portfolio review, immutable trade proposals, trusted approval requests, identifier-only execution, reconciliation, and audit explanations. Use when a user asks TradingCat to analyze or follow a stock, validate a strategy, monitor buy/sell signals, review holdings, recommend target allocation, prepare a trade plan, request approval, execute an already approved plan, or inspect the system's research and execution evidence.
---

# TradingCat

Operate the TradingCat quantitative research and decision system through its stable interfaces.
Keep the interaction layer independent of QwenPaw, Codex, Trae, or any other Agent runtime.

## Read only what the task needs

- Read `docs/agent-integration.md` completely before executing any TradingCat operation; it is the
  authoritative intent-to-operation map, payload contract, result interpretation, and stop rules.
- Read `docs/usage.md` for commands, JSON payloads, and personal-investor workflows.
- Read `docs/architecture.md` before changing module boundaries, research gates, data semantics,
  approval behavior, or execution flow.
- Read `docs/deploy.md` for installation, scheduling, migration, and process isolation.
- Read `docs/live-trading-checklist.md` for any request involving real money or LIVE mode.
- Read `docs/acceptance.md` when reporting readiness or claiming the system is verified.

## Choose the interface

Resolve the directory containing this `SKILL.md` as `TS_ROOT`; never assume a QwenPaw path or
modify an Agent container. Run subprocesses with `cwd=TS_ROOT`, argv arrays, JSON stdin, and
`shell=false`.

Use the JSON adapter for Agent integration and machine-readable results:

```bash
printf '{"query":"苹果"}' |
  ./.venv/bin/python -m application.cli analyze-security
```

Use `./tc` for human-facing and operational commands:

```bash
./tc --help
./tc market quote AAPL.US --json
```

Do not parse human-readable CLI text when a corresponding JSON contract exists.

Dispatch user intent as follows:

| Intent | Operation |
|---|---|
| Analyze a stock or strategy suitability | `analyze-security` |
| Follow a stock | `follow-security` |
| Review holdings and target weights | `review-portfolio` |
| Prepare a DRY_RUN plan | `propose-trade` |
| Prepare a LIVE plan for explicit approval | `propose-trade` with `mode=LIVE` |
| Request a pending approval | `request-approval` with `plan_id` and `plan_hash` |
| Submit an already approved plan | `execute` with `plan_id` and `confirmation_id` |
| Explain plan evidence | `explain-decision` |
| Cache, research, monitor, sync, reconcile, or back up | `./tc` operational command |

Do not expand an analysis request into follow, account sync, proposal, approval, or execution.

## Run a stock-analysis workflow

1. Resolve the requested name or symbol; do not guess when multiple securities match.
2. Ensure enough completed daily bars are cached.
3. Run prefilter and research when no current frozen strategy evidence exists.
4. Call `AnalyzeSecurity` and surface technical factors, research status, strategy suitability,
   data quality, warnings, and lineage.
5. State explicitly whether the result is `verified`, research-only, degraded, or blocked.
6. Never present missing fundamentals as zero or silently substitute current data into history.

Typical commands:

```bash
./tc research add AAPL.US
./tc research cache AAPL.US
./tc research prefilter AAPL.US
./tc research run AAPL.US --grid small
printf '{"query":"AAPL"}' |
  ./.venv/bin/python -m application.cli analyze-security
```

## Run a follow-and-monitor workflow

1. Add the security through `FollowSecurity` or `tc subscribe add`.
2. Explain that following does not assign a strategy or grant trade eligibility.
3. Use pre/intra/post monitoring for entry zones, exit conditions, stop movement, and missing
   protection alerts.
4. Report signals as observations requiring user review, never as automatic orders.

```bash
printf '{"query":"AAPL","reason":"等待趋势信号"}' |
  ./.venv/bin/python -m application.cli follow-security
./tc monitor pre --scope watchlist
./tc monitor intra --scope watchlist
./tc monitor post --scope watchlist
```

## Run a portfolio workflow

1. Sync account state when credentials and read-only access are available.
2. Call `ReviewPortfolio` for KEEP/ADD/REDUCE/EXIT, current weight, target range, stop, rationale,
   and risk flags.
3. Treat shrinkage Kelly as one upper bound, not an instruction to invest that amount.
4. Preserve portfolio constraints for concentration, correlation, sector, currency, beta,
   leverage, events, liquidity, purchasing power, and pending orders.
5. If account state is not SYNCED, return diagnostic advice only and state that LIVE is blocked.

## Prepare a trade proposal

1. Use `ProposeTrade` or `tc portfolio build --mode DRY_RUN`.
2. Show the immutable plan, `plan_hash`, evidence lineage, target weights, and risk result.
3. Make clear that a plan is not approval and not an order.
4. Use `RequestApproval` only to create a PENDING request.
5. Use `ExplainDecision` to answer where a plan came from; cite strategy, data, policy, and audit
   identifiers from the response rather than reconstructing them.

For an explicitly requested LIVE workflow, `ProposeTrade` with `mode=LIVE` creates an immutable
plan and returns `PENDING_APPROVAL`; it does not approve or submit anything. Then call
`RequestApproval` with the returned `plan_id` and `plan_hash`. A trusted human approval adapter
must produce the signed `ApprovalProof`; the Agent must not create, copy, or replay that proof.
Only after trusted approval is recorded may the Agent call `execute` with exactly `plan_id` and
`confirmation_id`. `execute` never accepts symbol, side, quantity, price, or mode overrides.

## Enforce execution invariants

- Never let an Agent, strategy, monitor, scheduler, or ordinary CLI mint LIVE `APPROVED`.
- Never call the real broker outside the isolated executiond safety chain.
- Bind approval to the exact immutable `plan_hash`; changed plans require new approval.
- Run PreTradeRisk after approval; it may only PASS or REJECT, never alter approved fields.
- Consume Confirmation and create all OrderIntents atomically and idempotently.
- Block new LIVE work for non-SYNCED accounts, UNKNOWN orders, MISMATCH reconciliation, expired
  proofs, proof replay, or a closed Canary.
- Keep normal operation in `DRY_RUN`/`PAPER`. The LIVE software path supports proposal, trusted
  approval, executiond execution, broker events, and reconciliation, but production real orders
  still require explicit P0-B scope and completion of `docs/live-trading-checklist.md` in the
  target deployment environment.

## Respect data boundaries

- Use `shared/longbridge_client.py` as the only Longbridge Python SDK wrapper.
- Require `longbridge==4.4.3`; do not invoke Longbridge CLI/OAuth.
- Detect optional Longbridge fundamental capabilities at runtime and fail safe when unavailable.
- Use optional OpenAlice JSON-stdio data only as a current snapshot.
- Admit historical fundamentals only with explicit `period_end`, `published_at`, `available_at`,
  and `source`; otherwise fail closed.
- Use completed bars, preserve adjustment/source identity, and replace rather than splice history
  when changing providers.
- Use StateRepository APIs in `shared/db.py`; do not open SQLite directly from business modules.

## Preserve research integrity

- Use nested Walk-Forward for selection and outer-OOS evaluation.
- Expose Final Holdout only after freezing the candidate and only once per candidate identity.
- Include realistic costs and reject unresolved stop-limit or data-quality failures.
- Size positions only from frozen OOS evidence; insufficient evidence means no new entry plan.
- Treat Longbridge Quant, Qlib, and vectorbt as optional research tools, never as automatic
  production eligibility.

## Verify changes

Run focused tests while editing, then finish with:

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python e2e_full.py
./.venv/bin/python scripts/acceptance_v5.py
```

Never run a real order as part of automated verification. Report degraded optional capabilities
separately from failures in the core research and safety chain.
