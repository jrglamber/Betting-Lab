# Betting Lab v0.6.2 — One-click Research Export

Added a dashboard button and `/export/research.zip`.

The export is generated from the live Postgres database and includes both
derived research summaries and the underlying CSV research tables so the ZIP
can be uploaded into ChatGPT for analysis.

No betting, signal, execution-shadow, quota or settlement rules are changed.

Security:
API key, database URL and admin secret are deliberately excluded.
