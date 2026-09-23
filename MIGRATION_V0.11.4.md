# Betting Lab v0.11.4 — Predictive PostgreSQL LIKE Hotfix

No-reset hotfix over v0.11.3.

No schema change.

Fixes psycopg2 `tuple index out of range` caused by literal `%` characters in
three PRED1 SQL LIKE clauses. All are now parameterized safely.

Do not reset Postgres.
`ENABLE_LIVE_BETTING=false`.
