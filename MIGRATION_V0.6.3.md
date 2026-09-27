# Betting Lab v0.6.3 — Efficiency + Calibration

Changes requested after the first v0.6.2 research export.

Collection:
- stop requesting DNB by default;
- preserve broad all-bookmaker discovery for consensus;
- convergence calls use only one relevant market;
- convergence calls use only Betfair Exchange UK / Matchbook / Smarkets
  (or the configured execution venue list);
- quota is checked before every individual paid call;
- convergence calls are audited as `ODDS_CONVERGENCE`.

Research:
- odds-band segmentation;
- time-to-kickoff segmentation;
- model-edge vs final-CLV calibration;
- fixture exposure/correlation analysis.

Unchanged:
- signal thresholds;
- canonical rules;
- approved execution requirements;
- automatic settlement;
- live betting remains disabled;
- existing Postgres history is retained.
