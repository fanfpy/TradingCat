## Summary

Describe the problem and the bounded change.

## Safety and data boundaries

- [ ] No credentials, account data, positions, broker order IDs, databases, or reports are included.
- [ ] Historical data keeps explicit source/adjustment/PIT semantics.
- [ ] Agent, approval, PreTradeRisk, and executiond boundaries remain intact.
- [ ] No automated test or scheduler can place a real order.

## Verification

List the exact commands and results.

- [ ] `python scripts/check_open_source.py`
- [ ] `python -m pytest -q`
- [ ] `python e2e_full.py`
- [ ] Documentation and Agent contract updated when applicable.

## Compatibility

Describe CLI, JSON schema, database migration, and Longbridge SDK impact.
