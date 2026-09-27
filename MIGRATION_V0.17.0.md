# Migration — Betting Lab v0.17.0

Deploy directly over v0.16.1. **Do not reset Postgres.**

## What changes

v0.17.0 adds two isolated research layers:

1. **Outcome Edge** — observed win rate versus entry-implied probability, by odds band and other cohorts. CLV and ROI remain supporting diagnostics. No selection/execution authority.
2. **PXG1** — free proxy-xG research. StatsBomb event data is reprocessed into ordinary match-stat features and a chronological held-out ridge model is trained against genuine StatsBomb xG. Optional API-Football current match statistics can then be scored by the same frozen mapping.

Existing PRED1, PRED2, PRED3, META1, historical validation, MS1/MS2, MSP1/MSP2, Tennis, signals, price paths, settlement and all existing tables remain in place.

## New database tables

- `football_pxg_statsbomb_manifest`
- `football_pxg_statsbomb_samples`
- `football_pxg_models`
- `football_pxg_api_discovery_days`
- `football_pxg_api_manifest`
- `football_pxg_current_matches`
- `football_pxg_api_usage`

All are additive `CREATE TABLE IF NOT EXISTS` migrations.

## New routes

- `/proxy-xg`
- `/api/proxy-xg/status`
- `/admin/proxy-xg/run`
- `/outcome-edge`
- `/api/outcome-edge`

## Railway variables

No new variable is mandatory.

Without an API-Football key, PXG1 still backfills/trains its free StatsBomb proxy and current-data collection remains safely idle.

To enable free current completed-match statistics:

```text
API_FOOTBALL_KEY=<API-Football key>
```

Optional defaults:

```text
PROXY_XG_ENABLED=true
PROXY_XG_STATSBOMB_MATCHES_PER_CYCLE=12
PROXY_XG_MIN_TRAINING_SAMPLES=200
PROXY_XG_REFIT_EVERY_SAMPLES=40
PROXY_XG_RIDGE_ALPHA=3.0
PROXY_XG_API_DAILY_CALL_BUDGET=90
PROXY_XG_API_PROVIDER_RESERVE=5
PROXY_XG_API_BACKFILL_DAYS=45
PROXY_XG_API_MATCHES_PER_CYCLE=4
```

## Safety / research gates

- PXG1 is research-only and cannot create a bet.
- API-Football collection is fail-closed when no key is configured.
- The API collector has its own 90-call/day default guard and reads provider quota headers.
- Only current Betting Lab football competitions are admitted to the current-stat queue.
- Current matches with incomplete core shot statistics are rejected rather than zero-filled.
- API keys are not placed in URLs, are redacted from errors, and are excluded from exports.
- The proxy model reports held-out chronological performance against a constant-xG baseline before any future promotion decision.

## Validation

- Existing regression suite retained.
- New PXG extraction, API-Football parsing, league mapping, ridge validation, fail-closed key gate and schema tests added.
- Fresh SQLite database smoke checks cover `/`, `/proxy-xg`, `/outcome-edge`, `/predictive-football-pred3`, status APIs and research export generation.
