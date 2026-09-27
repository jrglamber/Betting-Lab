# Betting Lab v0.6.8 — Multiples Shadow

Additive migration only. No existing rows are deleted or reset.

New tables:
- `multiple_shadow_bets`
- `multiple_shadow_legs`
- `multiple_shadow_state`

`multiple_shadow_state.started_at` records when this forward-only research layer
was first initialized. Historical multiples are deliberately not reconstructed.

No changes to signal generation, canonical clustering, execution acceptance,
quota handling, provider collection, singles CLV, singles settlement, or live
betting status.
