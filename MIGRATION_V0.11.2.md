# Betting Lab v0.11.2 — Predictive Bootstrap Startup Guarantee

No-reset operational hotfix over v0.11.1.

No schema change.

PRED1 now runs one paced historical-score bootstrap batch during application
startup, independently of the main Odds API worker. Existing rows are preserved.

Do not reset Postgres.
`ENABLE_LIVE_BETTING=false`.
