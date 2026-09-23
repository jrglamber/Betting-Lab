# Betting Lab v0.8.1 — Multi-Sport Mechanics Hardening

Additive, non-destructive migration.

Adds to `multisport_execution_bets`:
- `settlement_quality TEXT`
- `settlement_provenance TEXT`

Existing v0.8.0 rows are preserved.

No football, tennis, multiples, quota or historical research rows are reset.
The cross-lane accounting fix only changes how an explicit provider-reported
zero request cost is recorded internally.

Default new setting:
`MULTISPORT_HOCKEY_REFERENCE_REGION=us`
