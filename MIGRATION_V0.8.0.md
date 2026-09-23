# Betting Lab v0.8.0 — Multi-Sport Shadow

Additive, non-destructive migration.

New isolated tables:
- multisport_league_state
- multisport_events
- multisport_odds_snapshots
- multisport_consensus_snapshots
- multisport_execution_evaluations
- multisport_execution_bets
- multisport_price_observations
- multisport_results

No existing football, tennis, multiples, quota, research or settlement rows
are reset or rewritten.

MSP1 begins forward-only on deployment. Out-of-season configured sport keys are
retained as inactive state and generate no paid odds calls.

`ENABLE_LIVE_BETTING` remains false.
