# Betting Lab v0.7.2 — Operational Hotfix

No schema migration is required.

This release:
1. fixes the background Worker call into the v0.7.1 Multiples API gate;
2. promotes severely overdue breadth inside 24h to scheduler urgency.

All existing Postgres rows are retained. Historical non-API MS1 rows remain
preserved and continue to settle/finalize closing evidence.

`MULTIPLES_API_BOOKMAKER_KEYS` should remain blank until a venue's official API
is verified to support submission of the accumulator itself.
