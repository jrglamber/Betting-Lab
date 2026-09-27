# Migration — Betting Lab v0.18.1

Deploy directly over v0.17.0 or v0.18.0. Do not reset Postgres.

This release contains the full v0.18.0 META2 Frozen CLV Trust Model plus a PXG1 backfill recovery fix. API-Football discovery-day rows that failed three times under the old Free-plan date restriction or transient provider timeouts can now be retried after a six-hour cooldown, up to six total attempts. Permanent errors remain blocked.

No new Railway variables are required. Existing `PROXY_XG_API_DAILY_CALL_BUDGET=1000` and `PROXY_XG_API_MATCHES_PER_CYCLE=12` can remain in place. No live betting authority is added.
