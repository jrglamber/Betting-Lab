# Betting Lab v0.5.1 — Automatic results

This release preserves all v0.5.0 data and strategy logic.

New behaviour:
- final football scores are fetched only for fixtures with unresolved shadow signals
- final score is persisted in `event_results`
- all OPEN signals on that event are automatically settled
- result types: WIN / LOSS / PUSH / VOID
- one-unit P&L is calculated from the original offered price
- event is marked COMPLETED
- dashboard exposes settled count, stored results and P&L

Quota:
- completed-score requests use `daysFrom=1` and cost 2 provider credits
- requests are batched by sport and event IDs
- daily budget + 50-credit reserve still apply
