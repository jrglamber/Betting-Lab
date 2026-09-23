# Migration — Betting Lab v0.18.0

Deploy directly over **v0.17.0**. Do not reset Postgres.

## What changes

- Adds META2 frozen CLV-trust shadow model after META1 reaches the clean A/B-label gate.
- Uses a chronological holdout before the final frozen forward model is created.
- Adds probability of beating close, expected CLV and trust-band annotations to future META1 samples.
- Adds `meta_edge_model_runs` and `meta_edge_model_scores` tables.
- Adds META2 dashboard/API/export visibility.

## What does not change

PRED1/PRED2/PRED3, PXG1, Outcome Edge, MS2, MSP1/MSP2, Tennis, odds collection, thresholds and execution-shadow logic are unchanged. META2 has no betting authority and makes zero provider calls.

## Railway

No new variable is required. Optional:

```text
META_EDGE_MODEL_ENABLED=true
```

The existing `META_EDGE_MIN_CLEAN_LABELS` threshold (default 200) controls when the first frozen META2 model is created. Once created, it does not self-retrain.
