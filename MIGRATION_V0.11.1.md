# Betting Lab v0.11.1 — Predictive Bootstrap Operational Hotfix

Additive/no-reset deployment over v0.11.0.

No database schema change is required.

Operational changes only:
- PRED1 runs first in each worker cycle.
- Predictive Football exposes bootstrap source health and errors.
- Research export includes bootstrap health.

Do not reset Postgres.
`ENABLE_LIVE_BETTING` remains false.
