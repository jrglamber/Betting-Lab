# Betting Lab v0.6.9 — Research Instrumentation

This is an additive Postgres/SQLite migration. No rows are deleted or reset.

New measurement fields are added to:
- `execution_shadow_bets`
- `multiple_shadow_bets`
- `event_results`

Historical rows are backfilled only where the measurement can be reconstructed
faithfully from already-stored odds/results (for example entry market quality,
broad reference closes and settlement provenance). Historical experiment
fingerprints are intentionally left blank because the exact old Railway
configuration cannot be proven retrospectively.

No new Odds API endpoints or calls are introduced by this release.
