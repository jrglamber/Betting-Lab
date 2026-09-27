# Betting Lab v0.5 migration

This is a drop-in replacement for the v0.4.2 flat GitHub layout.

No database reset is required. `db.init_schema()` creates the two new tables:
- `candidate_evaluations`
- `signal_price_observations`

Existing `events`, `odds_snapshots`, `consensus_snapshots`, `signals`, quota state
and collector history are preserved.

Behavioural correction: v0.5 only finalises `closing_odds` / `clv_pct` after the
fixture kickoff time has passed. Before kickoff, follow-up prices are stored as
price observations instead.

On startup v0.5 also clears any pre-v0.5 `closing_odds`/`clv_pct` that were incorrectly written for fixtures that have not kicked off yet.
