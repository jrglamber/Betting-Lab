# Betting Lab v0.10.0 — Predictive Football Shadow

Additive, non-destructive migration.

New tables:
- football_predictive_training_matches
- football_predictive_source_state
- football_predictive_predictions
- football_predictive_evaluations
- football_predictive_bets
- football_predictive_price_observations

No existing football, tennis, multi-sport, multiples, quota or research rows
are reset or rewritten.

PRED1 begins forward prediction/shadow evidence from deployment. Historical
scorelines are used only to warm model parameters; they are not retroactively
converted into historical PRED1 bets.

`ENABLE_LIVE_BETTING` remains false.
