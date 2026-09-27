# Betting Lab v0.13.0 — Predictive Evidence Expansion

This is a non-destructive research/measurement release on top of v0.12.0.

## What changes

- PRED1/PRED2 approved-venue price paths become progressively denser approaching kickoff.
- A broad h2h-only close-capture lane improves closing-consensus/Brier evidence without creating extra signals.
- A separate historical PRED1/PRED2 validator reconstructs past fixtures using only information available at the original forecast freeze time.
- Historical validation stores Brier/log-loss, broad close Brier, sampled approved-venue execution, CLV and commission-adjusted P&L.
- Research export includes all historical-validation tables and summary metrics.

## What does NOT change

- PRED1 Poisson specification.
- PRED2 Dixon-Coles specification or rho grid.
- 24h forward forecast freeze.
- 3% minimum / 5% strong edge thresholds.
- approved execution venues.
- first-acceptable forward execution freeze.
- live betting remains disabled.

## Recommended Railway variables for the 100k/month allowance

```text
DAILY_PAID_CREDIT_BUDGET=1000
MAX_EVENTS_PER_ODDS_CYCLE=3
PREDICTIVE_FOOTBALL_HIGH_RES_PRICE_PATH_ENABLED=true
PREDICTIVE_FOOTBALL_BROAD_CLOSE_ENABLED=true
PREDICTIVE_FOOTBALL_BROAD_CLOSE_HOURS_BEFORE=3
PREDICTIVE_FOOTBALL_HISTORICAL_ENABLED=true
PREDICTIVE_FOOTBALL_HISTORICAL_REGION=uk
PREDICTIVE_FOOTBALL_HISTORICAL_SNAPSHOT_MINUTES=1440,360,60,5
PREDICTIVE_FOOTBALL_HISTORICAL_DAILY_CREDIT_BUDGET=600
PREDICTIVE_FOOTBALL_HISTORICAL_INTERVAL_SECONDS=900
```

If `DAILY_PAID_CREDIT_BUDGET` or `MAX_EVENTS_PER_ODDS_CYCLE` already exists in Railway, the Railway value overrides the new code default. Update it explicitly if you want the denser forward collection.

## Database

Do **not** reset Postgres. v0.13.0 creates three additive tables:

- `football_predictive_historical_validations`
- `football_predictive_historical_odds`
- `football_predictive_historical_bets`

## Historical evidence caveat

Historical execution is sampled at fixed checkpoints (default 24h/6h/1h/5m). It is corroboration only and must not be presented as exact first-acceptable historical execution. Do not tune PRED1 or PRED2 from this cohort while the clean forward comparison is accumulating.
