<p align="center">
  <img src="static/tradingcat-icon.svg" alt="TradingCat project icon" width="180">
</p>

# TradingCat

[中文文档](README.md)

TradingCat is an agent-independent quantitative research and trade-decision system for individual
investors. It connects stock analysis, strategy validation, watchlist monitoring, shrinkage-Kelly
position guidance, portfolio risk, immutable trade plans, and explicit human approval without
depending on QwenPaw, Codex, Trae, or any other Agent runtime.

The project is currently **DRY_RUN_ONLY**. Research and real read-only market data have been
validated. A live order still requires an isolated execution service, a valid human
`ApprovalProof`, post-approval risk checks, and an explicitly scoped Live Canary. The ordinary CLI
cannot approve or submit a live order.

## Features

- Technical factor analysis, data-quality reporting, and strategy suitability.
- Cost-aware backtesting, nested walk-forward validation, robustness checks, and final holdout.
- Watchlists plus pre-market, intraday, and post-market signal monitoring.
- Shrinkage Kelly, stop-risk limits, target-weight ranges, and portfolio constraints.
- Immutable plans, approval proofs, idempotent order intents, broker events, and reconciliation.
- Stable JSON stdin/stdout contracts for arbitrary Agents.

## Quick start

Python 3.10 or 3.11 is recommended. The supported Longbridge SDK is pinned to `4.4.3`.

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Fill in one Longbridge Legacy API Key application's three credentials.
./.venv/bin/python shared/sdk_diagnostics.py --connect
```

TradingCat never reads a Longbridge CLI OAuth token and never launches browser authentication.
Longbridge fundamental capabilities are detected at runtime; fundamentals therefore degrade explicitly
unless a qualified PIT provider is configured. Technical research and execution safety are not affected.

## Personal-investor workflow

```bash
# Research
./tc research add AAPL.US
./tc research cache AAPL.US
./tc research prefilter AAPL.US
./tc research run AAPL.US --grid small

# Follow and monitor
./tc subscribe add AAPL.US --push-daily
./tc monitor pre --scope watchlist
./tc monitor intra --scope watchlist
./tc monitor post --scope watchlist

# Portfolio diagnostics
./tc account sync
./tc position --symbol AAPL.US
./tc risk check

# Produce an immutable dry-run plan; this does not place an order
./tc portfolio build --equity 100000 --mode DRY_RUN
```

## Agent integration

Agents should use the JSON contract rather than parse terminal-oriented output:

```bash
printf '%s' '{"query":"Apple"}' |
  ./.venv/bin/python -m application.cli analyze-security

printf '%s' '{"query":"AAPL","reason":"Monitor validated signals"}' |
  ./.venv/bin/python -m application.cli follow-security

printf '%s' '{"account_id":"default"}' |
  ./.venv/bin/python -m application.cli review-portfolio
```

Every response uses the `tradingcat.v1` envelope with `ok`, `data`, `error`, `warnings`, and
`lineage`. An Agent may explain a result or request a pending approval, but it must not mint an
approval proof or bypass executiond.

See [Agent integration](docs/agent-integration.md) for the intent routing table, payload schemas,
result interpretation, and mandatory stop conditions.

## Safety invariants

1. Agents, strategies, monitors, and schedulers cannot call the live broker directly.
2. Every live order must bind to an immutable `ExecutionPlan.plan_hash` and real human proof.
3. Pre-trade risk runs again after approval; any changed plan requires new approval.
4. Unsynced accounts, UNKNOWN orders, or reconciliation mismatch block new live work.
5. Historical fundamentals require explicit period, publication, availability, and source times.

Read [the live-trading checklist](docs/live-trading-checklist.md) before any real-money work.

## Verification

Public/offline verification requires no broker credentials:

```bash
./.venv/bin/pip install -r requirements-dev.txt
TRADINGCAT_ENV_FILE=/tmp/tradingcat-no-env ./.venv/bin/python -m pytest -q
TRADINGCAT_ENV_FILE=/tmp/tradingcat-no-env ./.venv/bin/python e2e_full.py
./.venv/bin/python scripts/check_open_source.py
./.venv/bin/python -m build
./.venv/bin/python scripts/check_distribution.py
```

Real read-only acceptance additionally requires local Longbridge credentials:

```bash
./.venv/bin/python scripts/acceptance_v5.py
```

Acceptance never creates a Live Canary or places a real order.

## Documentation

- [Architecture](docs/architecture.md)
- [Agent integration](docs/agent-integration.md)
- [Usage guide](docs/usage.md)
- [Deployment](docs/deploy.md)
- [Live-trading checklist](docs/live-trading-checklist.md)
- [Acceptance evidence](docs/acceptance.md)
- [Open-source release guide](docs/open-source-release.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Disclaimer](DISCLAIMER.md)

## License

Licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for attribution information.

TradingCat is not affiliated with or endorsed by Longbridge or any referenced Agent or data
provider. This software is not investment advice.
