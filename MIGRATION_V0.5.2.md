# Betting Lab v0.5.2 — Canonical bet clustering

Problem:
Multiple strategy/bookmaker detections of the same underlying selection could
inflate headline P&L and drawdown as though they were independent bets.

Fix:
- preserve every raw detection in `signals`
- create one canonical bet per unique event/market/selection/line
- choose best price from the first actionable detection wave
- freeze canonical entry price
- count supporting strategies/bookmakers/detections
- use canonical bets for headline P&L, ROI, CLV and drawdown
- keep strategy scoreboards separately for research
- automatically backfill existing v0.5.1 data
