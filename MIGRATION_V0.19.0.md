# Migration — Betting Lab v0.19.0

Deploy directly over v0.18.1. **Do not reset Postgres.** All migrations are additive.

This release adds PRED4, an isolated current-PXG shadow challenger that consumes PXG1 current histories and makes zero provider calls. It also freezes PRED1/PRED2 BTTS and the 4.00–4.99 / 4.00–7.49 odds-band findings as forward-only Outcome Edge watch cohorts, and fixes Outcome Edge coverage so PRED derived-market bets are included correctly.

No existing predictive, execution, MS2, MSP1/MSP2, Tennis, META1/META2, historical validation or PXG1 thresholds are changed. No new Railway variables are required.
