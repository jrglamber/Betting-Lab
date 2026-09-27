# Betting Lab v0.16.0 — PRED3 StatsBomb xG Challenger

Deploy over v0.15.0. No database reset.

The migration is additive and creates separate PRED3 StatsBomb manifest, xG
training, forecast, evaluation, bet and price-path tables. PRED1/PRED2 tables and
model rules are unchanged.

StatsBomb Open Data ingestion is free and does not consume Odds API credits.
Network bootstrap is performed by the worker rather than FastAPI startup so the
web service is not blocked by GitHub latency.

PRED3 is research/shadow only and cannot place a live wager.

Default xG staleness guard: `PREDICTIVE_FOOTBALL_PRED3_MAX_DATA_AGE_DAYS=900`. Older open-data profiles are skipped rather than treated as current team evidence.
