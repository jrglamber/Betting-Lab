# Betting Lab v0.6.1 — Execution Shadow

User requirement: live betting is only worth pursuing if it can be automated.

Changes:
- approved execution venue list (default Betfair Exchange UK, Matchbook, Smarkets)
- all bookmakers retained for consensus/reference modelling
- theoretical canonical opportunities remain in `canonical_bets`
- new `execution_shadow_bets` table contains only automation-capable executable bets
- approved venue price must be >= the supporting strategy's minimum acceptable odds
- entry is frozen at first accepted execution wave
- execution rejection audit
- approved execution-venue agreement count for research
- approved-venue price-path tracking and final CLV
- automatic result settlement for execution shadows
- headline P&L/ROI/CLV/research intelligence switched to execution-shadow universe
- historical Postgres data backfilled without reset
- live order placement remains disabled
