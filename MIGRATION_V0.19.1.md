# Betting Lab v0.19.1 — MS3 Cohort Systems Shadow

Deploy directly over v0.18.0/v0.18.1/v0.19.0. Do **not** reset Postgres.

This release preserves every existing lane and adds a new forward-only MS3 research lane:

- BTTS Yankee / Heinz
- 4.00–7.49 Yankee / Heinz
- Hybrid Yankee (3 BTTS + 1 odds-band)
- Hybrid Heinz (4 BTTS + 2 odds-band)

Each exact selection is used once per cohort/system experiment, different fixtures only, and cards form from fresh already-stored executable constituent prices. MS3 makes no provider calls and can never place an order. A 1u synthetic system is compared with the same 1u constituent-singles control.

System prices are explicitly synthetic constituent-price products until a legitimate native multiple quote source is available.

No new Railway variables are required.
