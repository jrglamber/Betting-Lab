# Betting Lab v0.13.1 — PostgreSQL Historical Summary Hotfix

- No database reset or destructive migration.
- Replace v0.13.0 application files with v0.13.1.
- No Railway variable changes are required.
- Fixes HTTP 500 on dashboard/status caused by a literal `%` LIKE wildcard in the new historical summary query.
- PRED1, PRED2 and all v0.13.0 evidence-collection rules are unchanged.
