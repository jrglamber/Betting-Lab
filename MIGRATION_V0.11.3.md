# Betting Lab v0.11.3 — Predictive Bootstrap Source Fix

No-reset operational hotfix over v0.11.2.

No schema change.

Changes:
- historical bootstrap no longer requires `SPORT_KEYS` to contain matching
  football-data league keys;
- observed Betting Lab football leagues are prioritised;
- all supported bootstrap leagues remain eligible as fallback;
- bootstrap runs before internal-result sync during startup;
- predictive status exposes target sports and latest startup run/error.

Do not reset Postgres.
`ENABLE_LIVE_BETTING=false`.
