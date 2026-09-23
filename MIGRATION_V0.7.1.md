# Betting Lab v0.7.1 — Multiples API Gate

Additive, non-destructive migration.

Adds to `multiple_shadow_bets`:
- `automation_eligible INTEGER NOT NULL DEFAULT 0`
- `venue_policy TEXT`
- `allowed_api_bookmakers_json TEXT`

Existing MS1 rows receive the default `automation_eligible=0` and are preserved
unchanged otherwise. They continue to settle and collect closing evidence.

New multiple formation is paused unless `MULTIPLES_API_BOOKMAKER_KEYS` contains
at least one explicitly verified accumulator-capable official API venue.

No Postgres reset or historical deletion is performed.
