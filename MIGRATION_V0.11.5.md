# Betting Lab v0.11.5 — Predictive Execution Polling Fix

No-reset operational fix over v0.11.4.

No schema change.

Frozen PRED1 forecasts and derived BTTS/totals cases now enter approved-venue
convergence polling before a shadow bet exists. This removes the circular
dependency that left the Execution Funnel empty.

Do not reset Postgres.
`ENABLE_LIVE_BETTING=false`.
