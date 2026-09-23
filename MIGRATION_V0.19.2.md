# Migration v0.19.2

Deploy directly over v0.19.1 (or the current v0.18.x/v0.19.x database).

- No Postgres reset.
- No required new Railway variables.
- Adds nullable MS3 audit columns: `armed_at`, `confirmed_at`, `rejected_at`, `rejection_reason`.
- Preserves the existing `cohort_system_shadow_state.started_at` forward-test timestamp.
- MS3 now arms one candidate card, priority-refreshes each exact constituent event/market through approved-venue convergence polling, then confirms only if all post-arm prices remain eligible.
- This may consume a small number of additional Odds API credits while a card is armed.
- No live betting/order authority is added.
