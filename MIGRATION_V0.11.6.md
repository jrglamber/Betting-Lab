# Betting Lab v0.11.6 — Predictive Result Settlement Coverage

No-reset operational fix over v0.11.5.

No schema change.

The football result collector now requests completed scores for PRED1-only
fixtures as well as fixtures with original open signals. This fixes predictive
forecasts/shadows remaining open solely because the legacy signal engine did
not select the same match.

Do not reset Postgres.
`ENABLE_LIVE_BETTING=false`.
