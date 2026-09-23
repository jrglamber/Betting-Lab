# Betting Lab v0.16.1 — Historical Provider + Bootstrap Hotfix

This release rolls forward the complete v0.16.0 PRED3 StatsBomb xG challenger and fixes two operational issues found in the 2026-09-17 research export.

## Fixed: historical Odds API HTTP 422

The historical-events endpoint was being called with parameters that are valid on other Odds API endpoints but are not accepted by the current historical-events route (`dateFormat`, `commenceTimeFrom`, `commenceTimeTo`, and optional event IDs). The provider returned HTTP 422 before any historical validation odds could be collected.

v0.16.1 now:
- sends only the supported historical snapshot `date` parameter to `/historical/sports/{sport}/events`;
- normalizes historical timestamps to canonical UTC `...Z` form;
- recreates the desired kickoff-date window locally before team-name matching;
- retains the existing historical event-odds checkpoint collection at 24h / 6h / 1h / 5m.

No historical validation methodology or PRED1/PRED2 model rule is changed.

## Fixed: football-data.co.uk redirect to localhost

Recent Railway logs showed the free PRED score bootstrap request being redirected from the previous `www` URL to `127.0.0.1`, causing repeated connection failures.

v0.16.1 now:
- starts from the canonical `https://football-data.co.uk/mmz4281/...` host;
- disables automatic redirect following for this bootstrap request;
- follows redirects only when they remain HTTPS and on `football-data.co.uk` / `www.football-data.co.uk`;
- explicitly blocks redirects to localhost or any unrelated host.

The existing 7k+ score training history and internal-result sync are preserved. No database reset is required.

## Preserved from v0.16.0

- PRED3 StatsBomb xG/event challenger;
- PRED1/PRED2 frozen controls;
- META1 CLV trust research;
- MS2 manual Yankee/Heinz shadow lane;
- MSP1/MSP2, Tennis and all existing research/export/dashboard functionality;
- `ENABLE_LIVE_BETTING=false`.

## Validation

- full regression suite: 257/257 passing;
- fresh SQLite schema/startup smoke test passed;
- `/`, `/health`, `/predictive-football-pred3`, PRED3 API and historical-validation API all returned HTTP 200.
