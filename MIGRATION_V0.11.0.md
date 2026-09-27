# Betting Lab v0.11.0 — Predictive Football Market Expansion

Additive, non-destructive migration from v0.10.0.

New tables:
- football_predictive_market_predictions
- football_predictive_market_evaluations
- football_predictive_market_bets
- football_predictive_market_price_observations

Existing PRED1 1X2 forecasts and shadows are preserved unchanged. BTTS and
totals probabilities are derived from each already-frozen PRED1 expected-goal
pair, so upgrading does not refit those forecasts using later information.

Only future/open fixtures can create new BTTS/totals shadow bets. Historical
completed fixtures are not retroactively turned into executable shadows.

No database reset is required or permitted.
`ENABLE_LIVE_BETTING` remains false.
