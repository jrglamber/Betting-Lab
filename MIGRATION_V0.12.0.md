# Betting Lab v0.12.0 — PRED2 Dixon-Coles Challenger

No-reset release over v0.11.6.

## What changes

- Keeps PRED1 unchanged as the control model.
- Adds **PRED2_DIXON_COLES_V1** as an isolated forward challenger.
- PRED2 uses the same score-only training history, lookback, time decay,
  Bayesian/shrunk attack-defence strengths and 24h freeze as PRED1.
- PRED2 fits one Dixon-Coles low-score dependency parameter (`rho`) from
  pre-kickoff league history, then uses the corrected score grid for 1X2,
  BTTS and O/U 1.5/2.5/3.5 probabilities.
- PRED2 uses the same 3% entry gate and 5% strong-candidate threshold.
- PRED2 shadows, evaluations, CLV, Brier and P&L are stored in separate
  `football_predictive2_*` tables so evidence never contaminates PRED1.
- PRED1 and PRED2 convergence candidates are deduplicated by event + market,
  so paired forecasts reuse the same approved-venue quote wave rather than
  deliberately doubling the provider request.
- Result collection now also treats an unsettled PRED2 forecast as sufficient
  reason to retrieve the fixture result.
- Research export includes all PRED2 tables and summary metrics.
- Adds `/predictive-football-pred2` and `/api/predictive-football/compare`.

## New optional Railway variables

No new variable is required; defaults activate the challenger:

```text
PREDICTIVE_FOOTBALL_PRED2_ENABLED=true
PREDICTIVE_FOOTBALL_PRED2_RHO_MIN=-0.20
PREDICTIVE_FOOTBALL_PRED2_RHO_MAX=0.20
PREDICTIVE_FOOTBALL_PRED2_RHO_STEP=0.01
```

These are intentionally frozen research controls. Do not tune them from early
P&L or CLV.

## Database / live safety

- **Do not reset Postgres.** New tables are created with `CREATE TABLE IF NOT EXISTS`.
- No existing PRED1 table is rewritten or cleared.
- `ENABLE_LIVE_BETTING=false` remains required.
- No live order-placement path is added.
