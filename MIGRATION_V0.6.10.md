# Betting Lab v0.6.10 — Collection Hardening

Additive/non-destructive migration. No betting or research rows are deleted or reset.

Adds event-level odds-collection quarantine metadata:
- `odds_quarantine_until`
- `odds_failure_count`
- `odds_last_failure_code`
- `odds_last_failure_at`

Historical collector error details containing `apiKey=` are scrubbed in place during schema initialization.

Collection scheduling now paces the configured daily breadth target across the day, prioritizes imminent/overdue fixtures, and interleaves urgent breadth with convergence.

No signal, fair-value, execution, MS1, settlement, CLV, quota, or live-betting rules change.
