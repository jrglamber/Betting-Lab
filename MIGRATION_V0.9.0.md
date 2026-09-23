# Betting Lab v0.9.0 — Multi-Sport Market Expansion

Additive, non-destructive migration.

New tables:
- multisport_lines_state
- multisport_line_odds_snapshots
- multisport_line_consensus_snapshots
- multisport_line_evaluations
- multisport_line_bets
- multisport_line_price_observations

Existing football, Tennis TS1, MSP1 moneyline, Multiples, results and research
rows are preserved. MSP2 begins forward-only.

`ENABLE_LIVE_BETTING` remains false.
