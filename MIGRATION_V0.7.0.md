# Betting Lab v0.7.0 — Tennis Shadow

Additive migration only. No existing football, multiples or research rows are
deleted, rewritten or reset.

New isolated tables:
- tennis_tournament_state
- tennis_events
- tennis_odds_snapshots
- tennis_consensus_snapshots
- tennis_execution_evaluations
- tennis_execution_bets
- tennis_price_observations
- tennis_results

No historical tennis backfill is performed. TS1 evidence begins forward from
deployment.

Football signals, canonical clustering, football execution acceptance,
Multiples MS1, football collection scheduler, quota logic and result grading are
unchanged.
