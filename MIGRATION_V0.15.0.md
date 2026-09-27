# Betting Lab v0.15.0 — META1 CLV Trust Research

Research/measurement release only. PRED1, PRED2, MS1, MS2 and all existing
execution thresholds remain unchanged.

## What is new

META1 creates `meta_edge_samples`, one forward-safe feature row per PRED1/PRED2
shadow bet. The row is frozen from information available at entry and labelled
later from the bet's closing-price outcome.

Features include model agreement, entry market depth/dispersion/overround,
claimed edge, time-to-kickoff, training support, uncertainty and match
archetype. Headline labels use only A/B-quality CLV.

The layer is observation-only:
- no bet is created, blocked, resized or promoted by META1;
- no provider request is made by META1;
- early results are not tuned;
- predictive meta-modelling is gated until `META_EDGE_MIN_CLEAN_LABELS` (default
  200) clean A/B labels exist.

## Look-ahead protection

Entry market features never fall forward to a post-entry odds wave. PRED1/PRED2
agreement is used only when the paired forecast already existed by the source
bet timestamp. This prevents the initial PRED2 catch-up cohort from becoming
future information for older PRED1 entries.

## Routes

- `/meta-edge`
- `/api/meta-edge/status`
- `/api/meta-edge/segments`
- `/api/meta-edge/samples`
- `POST /admin/meta-edge/run`

## New settings

```text
META_EDGE_ENABLED=true
META_EDGE_MIN_CLEAN_LABELS=200
```

## Data requirements

No new data provider is required for META1. It uses already-stored PRED1/PRED2
outputs plus Odds API entry/closing observations. A later xG/style/line-up PRED3
would require a separate football event-data source and is intentionally not
included here.

No database reset is required. `ENABLE_LIVE_BETTING=false`.
