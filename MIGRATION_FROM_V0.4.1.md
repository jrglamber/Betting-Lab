# v0.4.1 -> v0.4.2

Railway logs showed that the GitHub browser upload flattened `betting_lab/`
and `tests/` into repository root. The application therefore crashed before
startup with `ModuleNotFoundError: No module named 'betting_lab'`.

v0.4.2 intentionally adopts that flat layout.

Strategy/research functionality is unchanged:
- shadow-only
- Premier League + Championship
- 1X2 / O-U / BTTS / DNB
- quota guard
- fair-value consensus
- slow-book engine
- cross-market relative value
- CLV ledger
- Postgres/Railway support
