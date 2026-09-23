# Project Exit Plan — Betting Lab v0.19.2

Execution Shadow release.

Betting Lab remains **100% shadow-only**. No real orders are placed.

## v0.19.2 — MS3 arm → refresh → confirm hardening

v0.19.2 keeps every betting hypothesis, threshold and forward-test start point unchanged. It fixes only MS3 card formation.

- A prospective Yankee/Heinz now first enters `ARMED` state from the recent eligible candidate pool (candidate quotes may be up to six hours old; they are **not** used as final entry prices).
- While a card is armed, its exact event/market pairs receive priority approved-venue convergence refreshes through the existing Odds API collector.
- The card becomes `OPEN` only after every constituent has a **post-arm** quote no more than 45 minutes old and every leg still satisfies its frozen minimum price / 4.00–7.49 role where applicable.
- If any refreshed leg is no longer eligible, the arm is rejected. If confirmation is not completed within 45 minutes or the first kickoff arrives, the arm expires. Rejected/expired arms are retained for audit but are excluded from system performance metrics.
- Only one prospective card is armed globally at a time. With the normal three-event odds cycle this prevents a confirmation backlog; the six cohort/system experiments rotate deterministically so one lane cannot starve the others.
- Confirmed cards still use 1u total system stake versus the same 1u split across singles, and system prices remain synthetic products of confirmed constituent singles prices.
- MS3 remains shadow-only. The confirmation refresh can add a small number of targeted Odds API calls; it never places an order.

No Postgres reset is required and the original MS3 `started_at` timestamp is preserved.


## v0.19.1 — MS3 Cohort Systems Shadow

Adds a forward-only systems experiment alongside the existing singles, MS1 and MS2 lanes. Nothing is promoted to live execution.

Frozen cohorts:

- BTTS Yankee: 4 unique PRED1/PRED2 BTTS legs.
- BTTS Heinz: 6 unique PRED1/PRED2 BTTS legs.
- Odds 4.00–7.49 Yankee/Heinz: 4 or 6 fresh eligible selections whose current stored executable constituent price is in that band.
- Hybrid Yankee: 3 BTTS + 1 odds-band leg.
- Hybrid Heinz: 4 BTTS + 2 odds-band legs.

Rules are deterministic and forward-only: different fixtures, fresh stored prices, constituent price still above the source strategy minimum, and a selection can be used only once within each cohort/system experiment. Cards freeze as soon as enough eligible legs exist. Each card risks 1u synthetically across its system lines and is compared with the same 1u split over its constituent singles.

MS3 makes **zero provider calls** and has **zero order-placement authority**. Its system odds are synthetic products of constituent singles prices, not native bookmaker multiple quotes. The `/cohort-systems` page and `/api/cohort-systems/*` endpoints expose the forward results.

## Hard execution rule

Only automation-capable approved venues can create headline/live-candidate bets:

```text
betfair_ex_uk
matchbook
smarkets
```

Configure with:

```text
EXECUTION_SHADOW_ENABLED=true
EXECUTION_BOOKMAKER_KEYS=betfair_ex_uk,matchbook,smarkets
```

Other bookmakers are **not discarded**. Their prices remain useful for:

- de-vigged consensus / fair-price construction
- slow-book comparisons
- cross-market modelling
- theoretical best-price reference

But they cannot create an `execution_shadow_bet`.

## Three-layer funnel

```text
raw detections (all books)
        ↓
theoretical canonical opportunity
        ↓
approved API venue has price >= minimum acceptable price?
        ↓
EXECUTABLE SHADOW BET / REJECT
```

The executable shadow ledger is now the headline research universe for:

- P&L / ROI
- CLV
- drawdown
- market / league / bookmaker segmentation
- edge calibration
- strategy agreement / approved execution-venue agreement
- weekly research reports

The old `canonical_bets` table is retained as theoretical research only.

## Execution realism

For each accepted execution shadow, the system stores:

- actual approved API venue
- executable entry odds
- minimum acceptable odds
- fair price / remaining edge
- best all-market reference price
- gap between executable price and theoretical best price
- number of approved execution venues with an acceptable price at entry
- price movement on the chosen execution venue
- final execution-venue CLV
- result and flat 1-unit P&L

If the theoretical signal exists but approved venues are too short, it records:

```text
EXECUTABLE_PRICE_BELOW_MIN
```

If no approved venue quote exists:

```text
NO_APPROVED_VENUE_QUOTE
```

## Backfill

Do **not** reset Postgres.

On startup v0.6.1 walks historical signal waves and odds snapshots and reconstructs which previous theoretical opportunities were genuinely executable on an approved venue at the time. Entry prices are frozen at the first approved/acceptable wave.

## New APIs

```text
/api/execution-bets
/api/execution/evaluations
/api/execution/scoreboard
```

`/research` now analyses executable shadow bets rather than theoretical canonical bets.

## Live betting

Still disabled:

```text
ENABLE_LIVE_BETTING=false
```

Actual API order placement will only be built after execution-shadow evidence is satisfactory.


## v0.6.2 Research Export

The main dashboard now contains:

```text
Download Research Export (.zip)
```

Route:

```text
/export/research.zip
```

The generated ZIP is built directly from the active Postgres research database
and contains:

- `analysis_summary.json`
- `manifest.json`
- `tables/events.csv`
- `tables/odds_snapshots.csv`
- `tables/consensus_snapshots.csv`
- `tables/signals.csv`
- `tables/canonical_bets.csv`
- `tables/execution_shadow_bets.csv`
- `tables/candidate_evaluations.csv`
- `tables/execution_evaluations.csv`
- both signal/execution price-observation tables
- results, weekly reports, collector runs and quota state

The export deliberately excludes:

- `ODDS_API_KEY`
- `DATABASE_URL`
- `ADMIN_SECRET`

This ZIP is intended to be downloaded and uploaded into ChatGPT for periodic
Betting Lab analysis.


## v0.6.3 Efficiency + Calibration

No signal threshold is changed.

### Collection changes

Discovery now requests only:

```text
h2h,totals,btts
```

`draw_no_bet` is filtered from an old `ODDS_MARKETS` Railway variable unless:

```text
ENABLE_DNB_MARKET=true
```

After an executable shadow exists, convergence polling no longer buys another
full all-bookmaker/all-market snapshot. It requests:

- only the relevant market for that execution shadow;
- only the approved automation-capable execution venues.

With the default three execution venues this is normally one credit per
convergence call rather than three credits for a fresh broad snapshot.

Broad snapshots remain all-UK-bookmaker inputs because those extra books improve
consensus/fair-price research without increasing the region count.

Quota is checked before every paid request, including each item in a multi-event
cycle.

### New research intelligence

`/research` and the research export now include:

- entry odds bands: `<2`, `2–3`, `3–5`, `5–8`, `8+`;
- time to kickoff: `<2h`, `2–6h`, `6–24h`, `1–3d`, `3d+`;
- explicit model-edge vs final-CLV calibration and optimism gap;
- fixture exposure/correlation analysis;
- performance grouped by one / two / three-plus bets on the same fixture.

Existing Postgres data is preserved.


## v0.6.6 Measurement + Accounting

Clean rebuild from known-working v0.6.3.

Added:
- CLV quality: A <=15m, B 15–30m, C 30–60m, STALE >60m.
- Headline CLV and edge-vs-CLV calibration use A+B only.
- Exact closing observation timestamp and minutes-before-kickoff retained.
- Historical league labels normalized from sport_key.
- Gross P&L retained, with estimated commission and net P&L/ROI alongside it.
- New fields are included in the dashboard, research intelligence and export.

Explicitly unchanged from v0.6.3:
- quota.py and quota behaviour;
- DAILY_PAID_CREDIT_BUDGET / QUOTA_RESERVE_CREDITS handling;
- signal thresholds and strategies;
- execution acceptance rules;
- broad/narrow polling mechanics;
- DNB collection setting;
- settlement grading;
- live betting remains disabled.


## v0.6.7 — Bets Until Midnight tile

Dashboard-only operational addition:
- new top-level tile: **Bets until midnight**
- counts OPEN executable-shadow bets whose fixture kickoff is between the
  current time and midnight in `Europe/London`
- multiple executable bets on the same fixture count separately
- exposed as `bets_until_midnight` in `/api/status`

No signal, execution, settlement, CLV, research, collection or quota logic has
been changed.


## v0.6.8 — Multiples Shadow

Adds a strictly downstream, research-only accumulator layer inside Betting Lab.
The primary singles engine is unchanged.

### MS1 formation rule
- doubles and trebles only
- source legs must already be OPEN executable-shadow singles
- different fixtures only
- all legs share the same UK kickoff date
- top 14 current source singles by modeled edge per UK date are considered
- every leg must have a stored quote no more than 30 minutes old at the SAME
  fixed-odds bookmaker
- that same-book quote must still meet the source single's frozen `min_odds`
- exchange single venues (`betfair_ex_uk`, `matchbook`, `smarkets`) are excluded
  from synthetic multiple formation
- first actionable same-book wave is frozen
- no additional Odds API/provider calls are made
- no historical multiple backfill is performed

### Measurement
The layer records:
- constituent execution-shadow IDs and frozen leg prices
- bookmaker and combined odds
- combined fair probability / fair odds / modeled edge
- same-book closing price for each leg
- combined CLV and worst-leg CLV quality
- automatic result settlement and flat-1u multiple P&L
- doubles/trebles, odds-band, bookmaker and market-mix splits
- maximum drawdown

A dedicated `/multiples` page and `/api/multiples/*` endpoints expose the side
show. Research exports include the multiples tables and summary.

This layer is not an execution claim: combined probabilities assume
independence between different fixtures, and current approved exchange APIs do
not imply that a bookmaker accumulator can be placed automatically.


## v0.6.9 — Research Instrumentation Freeze

Measurement-only release before the longer forward collection period.

Adds:
- permanent app / experiment / secret-free config fingerprints for new executable singles and MS1 multiples;
- entry market-quality capture: bookmaker count, median/best price, dispersion, chosen-vs-median and mean overround;
- broad-market closing benchmarks alongside the existing chosen-venue close;
- probability calibration (expected vs observed, Brier score, log loss and 10% probability bands);
- Multiples execution-realism fields: common-book count, common-book list, source pool size and entry quote timestamp spread;
- Multiples broad consensus and best-common-book closing benchmarks;
- source-leg reuse / overlap and effective-source-leg-count reporting;
- explicit result settlement provenance, with knockout-capable competitions flagged as regulation-time unverified;
- `/api/research/instrumentation` and export-summary instrumentation.

Credit impact:
- **zero new provider/API calls**
- all new measurements are calculated from rows already held in Postgres
- existing odds collection and existing results collection continue exactly as before

No changes to:
- signal generation / thresholds
- canonical clustering
- approved execution venues or acceptance rules
- Multiples MS1 formation rules
- collector cadence
- quota logic
- score polling
- live betting (`ENABLE_LIVE_BETTING` remains false)


## v0.6.10 — Collection Hardening

Collection/data-quality release only. Betting logic remains frozen.

Changes:
- paces `BREADTH_POLLS_PER_DAY` across the full UTC day instead of exhausting it early;
- prioritizes imminent and most-overdue fixtures ahead of distant never-polled events;
- interleaves urgent breadth and approved-venue convergence so neither lane starves;
- quarantines event-odds 404/410 failures with escalating cooldowns (2h, 6h, 12h, 24h);
- successful odds collection automatically clears the event failure/quarantine state;
- redacts Odds API credentials from future collector logs;
- scrubs historical leaked `apiKey=` values during schema initialization;
- defensively sanitizes all string fields in research exports;
- normalizes friendly league names for the full current 32-competition universe.

Unchanged:
- fair-value models and signal thresholds;
- canonical clustering;
- approved execution venues and first-acceptable execution freeze;
- Multiples MS1 construction rules;
- settlement and CLV rules;
- quota guard/daily budget/reserve logic;
- provider request endpoints and paid-call pricing;
- `ENABLE_LIVE_BETTING=false`.


## v0.7.0 — Tennis Shadow (TS1)

Adds an isolated tennis research lane inside Betting Lab.

TS1:
- discovers currently active tennis tournament sport keys from the provider's
  quota-free `/sports` endpoint;
- match winner (`h2h`) only;
- broad UK fixed-bookmaker prices are de-vigged bookmaker-by-bookmaker into a
  two-way consensus fair probability;
- Betfair Exchange UK / Matchbook / Smarkets remain the approved executable
  shadow venues;
- first approved price at or above the consensus-derived minimum acceptable
  odds is frozen;
- no tennis price can create or alter a football signal, canonical bet,
  execution shadow or Multiples MS1 bet;
- separate `/tennis` dashboard, APIs and research-export tables;
- A/B/C/STALE closing-price quality, CLV, probability calibration and
  commission-aware P&L;
- separate tennis paid-credit budget.

Default tennis variables:
- `TENNIS_SHADOW_ENABLED=true`
- `TENNIS_MARKET=h2h`
- `TENNIS_MIN_CONSENSUS_BOOKS=5`
- `TENNIS_MIN_EDGE_PCT=3.0`
- `TENNIS_DAILY_PAID_CREDIT_BUDGET=500`
- `TENNIS_QUOTA_RESERVE_CREDITS=1000`
- `TENNIS_MAX_CONSENSUS_AGE_MINUTES=240`
- `TENNIS_DISCOVERY_INTERVAL_SECONDS=21600`
- `TENNIS_RESULT_MIN_MINUTES_AFTER_START=90`
- `TENNIS_RESULT_POLL_INTERVAL_SECONDS=3600`

Tennis broad and convergence collection use the sport-wide odds endpoint, so a
single one-market request covers all currently returned matches in that
tournament. This is substantially more credit-efficient than polling every
tennis event individually.

`ENABLE_LIVE_BETTING` remains false. TS1 is shadow/research only.


## v0.7.1 — Multiples API Gate

Corrects the execution-realism gap in MS1.

Previously, MS1 deliberately used fresh same-book fixed-odds quotes from the
broad market to prove that a double/treble could theoretically have been
assembled at one conventional bookmaker. That was useful research, but those
bookmakers are not necessarily automation-capable.

From v0.7.1:

- NEW multiples can form only at bookmaker keys explicitly listed in
  `MULTIPLES_API_BOOKMAKER_KEYS`;
- the allowlist is EMPTY by default;
- a venue should be added only after its official API is verified to support
  submission of the accumulator/multiple itself — a singles betting API is not
  sufficient;
- with an empty allowlist, new multiple formation is intentionally paused;
- historical MS1 records are retained and continue to settle/finalize CLV;
- historical non-API MS1 rows have `automation_eligible=0`;
- new gated rows have `automation_eligible=1`,
  `venue_policy=EXPLICIT_VERIFIED_MULTIPLES_API_ALLOWLIST`, and store the
  allowlist used at formation;
- headline Multiples dashboard/API metrics show only automation-eligible rows;
- `/api/multiples/bets?include_legacy=true` exposes the preserved historical
  research rows when required;
- no football singles, canonical, execution, collection, settlement, Tennis
  Shadow or provider-credit logic is changed.

Default:
`MULTIPLES_API_BOOKMAKER_KEYS=`

Do not populate this variable merely because a venue has a singles API.


## v0.7.2 — Operational Hotfix

No betting-model changes.

Fixes:
- Worker now passes `MULTIPLES_API_BOOKMAKER_KEYS` to the v0.7.1
  accumulator/API gate instead of the removed `excluded_bookmaker_keys`
  argument.
- With the multiples API allowlist blank, formation remains intentionally
  paused while historical multiple CLV/result maintenance continues normally.
- Football breadth inside 24 hours of kickoff becomes urgent when its
  freshness debt is at least 2x the normal refresh interval. This prevents
  severely stale 12–24h-away fixtures being indefinitely displaced by
  convergence once the daily breadth pacing target is met.
- Existing <=6h breadth urgency and breadth/convergence alternation are
  preserved.

Unchanged:
- football fair-value, signals, canonical clustering and execution rules;
- football result grading / CLV logic;
- v0.7.1 multiples API eligibility policy;
- Tennis Shadow TS1;
- provider quota rules;
- `ENABLE_LIVE_BETTING=false`.

No database reset or destructive migration.


## v0.8.0 — Multi-Sport Shadow (MSP1)

Adds a new isolated two-way price-discovery cohort alongside Football and
Tennis TS1.

Default target sport keys:
- `baseball_mlb`
- `americanfootball_nfl`
- `americanfootball_ncaaf`
- `basketball_wnba`
- `aussierules_afl`
- `rugbyleague_nrl`
- `basketball_nba`
- `basketball_euroleague`
- `basketball_ncaab`
- `icehockey_nhl`
- `icehockey_sweden_hockey_league`
- `icehockey_sweden_allsvenskan`

The provider's quota-free `/sports` endpoint determines which configured
targets are active/in-season. Out-of-season leagues remain dormant and incur
no paid odds calls.

MSP1 hypothesis:
fixed-book two-way h2h/moneyline consensus -> de-vigged fair probability ->
approved exchange price -> first acceptable executable shadow -> A/B closing
price -> CLV/calibration/result.

Market integrity:
- match winner / moneyline (`h2h`) only;
- a bookmaker wave is used for consensus only if it contains exactly the two
  event teams/sides and no third outcome;
- approved execution waves must also be exactly two-way;
- this prevents a 3-way market from being compared with a two-way fair price;
- tied completed scores are retained as research `PUSH` outcomes and excluded
  from binary probability calibration.

New dashboard/API:
- `/multisport`
- `/api/multisport/status`
- `/api/multisport/bets`
- `/api/multisport/evaluations`
- `/api/multisport/leagues`
- `/admin/multisport/run`

Default variables:
- `MULTISPORT_SHADOW_ENABLED=true`
- `MULTISPORT_MARKET=h2h`
- `MULTISPORT_ODDS_REGION=uk`
- `MULTISPORT_MIN_CONSENSUS_BOOKS=5`
- `MULTISPORT_MIN_EDGE_PCT=3.0`
- `MULTISPORT_DAILY_PAID_CREDIT_BUDGET=1500`
- `MULTISPORT_QUOTA_RESERVE_CREDITS=1000`
- `MULTISPORT_MAX_CONSENSUS_AGE_MINUTES=240`
- `MULTISPORT_DISCOVERY_INTERVAL_SECONDS=21600`
- `MULTISPORT_RESULT_POLL_INTERVAL_SECONDS=3600`

MSP1 has its own paid-credit counter/lane. It shares only the provider's real
remaining balance/reserve with the other collectors.

Football strategy/collection/execution logic, Tennis TS1, Multiples API gate
and live-betting state are unchanged. No historical MSP1 backfill is
performed; evidence starts forward from deployment.


## v0.8.1 — Multi-Sport Mechanics Hardening

No strategy or edge-threshold changes.

Operational fixes:
- genuine provider `x-requests-last=0` responses remain zero-cost in internal
  football, football-results, Tennis TS1 and MSP1 ledgers;
- AFL tied two-runner match-odds shadows at currently verified dead-heat venues
  (`betfair_ex_uk`, `smarkets`) use `gross = offered_odds / 2 - 1` for a 1u
  research stake;
- an AFL tie at an execution venue whose dead-heat treatment has not been
  independently verified is tagged `UNVERIFIED_VENUE_RULE` and excluded from
  headline MSP1 P&L/ROI instead of being falsely treated as a push;
- MSP1 bets now retain `settlement_quality` and `settlement_provenance`;
- baseball settlements are explicitly tagged when listed-pitcher / void rules
  cannot be inferred from the odds-provider quote metadata;
- hockey breadth defaults to US two-way reference books through
  `MULTISPORT_HOCKEY_REFERENCE_REGION=us`; approved execution convergence
  continues to use the existing explicit exchange venue allowlist;
- exact-two-way validation remains mandatory, so 3-way hockey prices cannot
  enter two-way fair value.

New optional variable:
`MULTISPORT_HOCKEY_REFERENCE_REGION=us`

Football pricing/signals/canonical/execution rules, Tennis TS1 selection logic,
the Multiples API gate and `ENABLE_LIVE_BETTING=false` are unchanged.


## v0.9.0 — Multi-Sport Market Expansion

Adds MSP2 Lines Shadow as a separate research cohort for featured `spreads`
and `totals` markets. MSP1 moneylines remain unchanged.

MSP2 mechanics:
- active sports discovered quota-free;
- broad reference pricing uses US books for US sports/hockey and AU books for
  AFL/NRL;
- exactly matched featured lines only: no interpolation across different
  spreads/totals;
- minimum 3 reference books at the exact same line;
- same 3% minimum executable edge as MSP1;
- execution still restricted to approved API venues;
- first acceptable event/market/selection is frozen, preventing repeated bets
  as the featured line moves;
- line movement is tracked separately from price CLV;
- same-line price CLV is only calculated when the closing line is unchanged;
- spreads/totals settle from provider-completed final scores with provenance
  flags for rule-sensitive sports;
- MSP1 and MSP2 share the existing `MULTISPORT_DAILY_PAID_CREDIT_BUDGET`.

New default settings:
- `MULTISPORT_LINES_ENABLED=true`
- `MULTISPORT_LINES_MARKETS=spreads,totals`
- `MULTISPORT_LINES_MIN_CONSENSUS_BOOKS=3`
- `MULTISPORT_LINES_MIN_EDGE_PCT=3.0`
- `MULTISPORT_LINES_MAX_CONSENSUS_AGE_MINUTES=240`
- `MULTISPORT_LINES_US_REFERENCE_REGION=us`
- `MULTISPORT_LINES_AU_REFERENCE_REGION=au`

New page/API:
- `/multisport-lines`
- `/api/multisport-lines/status`
- `/api/multisport-lines/bets`
- `/api/multisport-lines/evaluations`
- `/api/multisport/funnel`

The research export now includes line odds, exact-line consensus, evaluations,
line shadows, line movement, and both MSP1/MSP2 execution-funnel summaries.

No historical MSP2 backfill is performed. Evidence begins forward from
v0.9.0 deployment. Live betting remains disabled.


## v0.10.0 — Predictive Football Shadow (PRED1)

Adds a genuinely different football edge experiment.

PRED1 does **not** use bookmaker prices to generate its forecast. It freezes an
independent 1X2 probability forecast from prior scorelines only, then checks
approved execution venues afterwards to see whether the independent model
finds an executable price and whether that price subsequently beats close.

Baseline model:
- Bayesian / prior-shrunk team attack and defence strengths;
- exponentially time-decayed historical scorelines;
- league-specific home/away scoring baselines;
- independent Poisson score matrix converted to 1X2 probabilities;
- one frozen forecast per fixture inside the configured pre-kickoff window;
- no bookmaker odds, consensus prices, CLV or future match data are model inputs.

Historical warm-up:
- current Betting Lab `event_results` are always synced into the training set;
- optional free football-data.co.uk CSV bootstrap supplies current/prior-season
  scorelines for supported major leagues;
- only Date, HomeTeam, AwayTeam, FTHG and FTAG are imported from those files;
- betting-odds columns from the historical files are deliberately ignored;
- external bootstrap requests use zero The Odds API credits and are paced over
  worker cycles so deployment/startup is not blocked;
- once Betting Lab has internal results for a league, those internal results
  become authoritative from that point forward to avoid double-counting.

Default PRED1 variables:
- `PREDICTIVE_FOOTBALL_ENABLED=true`
- `PREDICTIVE_FOOTBALL_BOOTSTRAP_ENABLED=true`
- `PREDICTIVE_FOOTBALL_BOOTSTRAP_REFRESH_SECONDS=86400`
- `PREDICTIVE_FOOTBALL_BOOTSTRAP_SOURCES_PER_CYCLE=4`
- `PREDICTIVE_FOOTBALL_FORECAST_HOURS_BEFORE=24`
- `PREDICTIVE_FOOTBALL_MIN_LEAGUE_MATCHES=40`
- `PREDICTIVE_FOOTBALL_MIN_TEAM_MATCHES=4`
- `PREDICTIVE_FOOTBALL_PRIOR_MATCHES=5`
- `PREDICTIVE_FOOTBALL_HALF_LIFE_DAYS=180`
- `PREDICTIVE_FOOTBALL_LOOKBACK_DAYS=550`
- `PREDICTIVE_FOOTBALL_MIN_EDGE_PCT=3.0`
- `PREDICTIVE_FOOTBALL_STRONG_EDGE_PCT=5.0`

Validation:
- every prediction records multi-class Brier score and log loss after result;
- when a clean closing consensus is available, PRED1 also compares its Brier
  score with the closing market's Brier score;
- executable model-value shadows retain first acceptable price, venue path,
  A/B/C/STALE CLV, result and commission-aware P&L.

New page/API:
- `/predictive-football`
- `/api/predictive-football/status`
- `/api/predictive-football/predictions`
- `/api/predictive-football/bets`
- `/api/predictive-football/evaluations`
- `/admin/predictive-football/run`

PRED1 is research only and cannot place a live bet. Football consensus,
Tennis TS1, MSP1, MSP2 and Multiples remain separate experiments.


## v0.11.0 — Predictive Football Market Expansion

PRED1 now uses the same frozen score forecast to test three market families:

- **1X2**
- **BTTS Yes / No**
- **Totals O/U 1.5, 2.5 and 3.5**

No second predictive model is fitted. The Bayesian/time-decayed Poisson model
still freezes the two expected-goal rates before any bookmaker/exchange price
is consulted. BTTS and totals probabilities are deterministic transforms of
those frozen lambdas.

Defaults:
- `PREDICTIVE_FOOTBALL_MARKETS=h2h,btts,totals`
- `PREDICTIVE_FOOTBALL_TOTAL_POINTS=1.5,2.5,3.5`

The additional markets use the same:
- `PREDICTIVE_FOOTBALL_MIN_EDGE_PCT=3.0`
- `PREDICTIVE_FOOTBALL_STRONG_EDGE_PCT=5.0`
- approved execution venues
- first-acceptable-price freeze
- A/B/C/STALE CLV classification
- commission-aware P&L.

Totals are exact-line only: an O2.5 model probability can never be matched to
an O3.5 execution quote or close. The first release deliberately uses half-goal
lines only, so settlement has no push state.

New research tables:
- `football_predictive_market_predictions`
- `football_predictive_market_evaluations`
- `football_predictive_market_bets`
- `football_predictive_market_price_observations`

The standard football collector now includes open PRED1 1X2/BTTS/totals
shadows in approved-venue convergence polling so their closing-price evidence
is not dependent only on breadth polling.

PRED1 remains shadow/research only. `ENABLE_LIVE_BETTING=false`.


## v0.11.1 — Predictive Bootstrap Operational Hotfix

Fixes an operational sequencing issue observed after v0.11.0 deployment:
PRED1 historical bootstrap was only reached at the end of the main worker
cycle, after football breadth, result maintenance, Tennis, MSP1 and MSP2.
On a busy deployment this could leave `Training matches = 0` for too long.

v0.11.1 moves PRED1 maintenance to the **front** of every worker cycle, so its
historical score bootstrap runs before the expensive collection lanes.

The Predictive Football page/API now also exposes:
- bootstrap sources attempted;
- bootstrap sources successful;
- bootstrap failures;
- historical rows imported;
- last bootstrap attempt;
- the latest sanitized bootstrap error, if any.

This means `Training matches = 0` is now immediately diagnosable rather than
looking like normal filtering.

No model rules, 3% entry threshold, 5% strong threshold, live settings, or
existing Betting Lab strategy mechanics are changed.


## v0.11.2 — Predictive Bootstrap Startup Guarantee

The v0.11.1 diagnostics exposed the remaining issue clearly: a page showing
`0/0 sources successful` and `last attempt not yet` means the bootstrap
function has never been invoked.

v0.11.2 guarantees that one paced bootstrap batch runs directly in FastAPI
startup whenever PRED1 is enabled. This does **not** depend on `RUN_WORKER` or
on the Odds API key because the historical score warm-up uses its own free
score source.

Startup sequence is now:

1. initialise/migrate DB;
2. sync any stored football results;
3. run one PRED1 historical bootstrap batch;
4. immediately freeze any eligible forecasts from the newly loaded history;
5. derive BTTS/totals probabilities;
6. continue normal worker cycles, which load the remaining historical sources.

The existing bootstrap limit remains in force, so startup makes only the paced
number of source requests rather than downloading the full league universe
before the web service can start.

No strategy threshold, model rule or live setting changes.


## v0.11.3 — Predictive Bootstrap Source Fix

The v0.11.2 runtime diagnostics showed:
- app version was correct;
- PRED1 was enabled;
- bootstrap was enabled;
- but bootstrap still showed `0/0` sources and `last attempt not yet`.

The root operational weakness was that historical source selection still
depended on the main `SPORT_KEYS` configuration. If that Railway variable is
blank, differs from the collector's actual league set, or uses a different
configuration path, PRED1 can legally enter the bootstrap function yet select
zero sources.

v0.11.3 removes that dependency.

Historical bootstrap targets are now prioritised in this order:
1. supported football sport keys already observed in the Betting Lab `events`
   table;
2. supported keys in `SPORT_KEYS`;
3. every football-data league mapping PRED1 knows how to import.

The source remains free and the existing per-cycle source limit remains in
place, so widening target discovery does not create Odds API cost.

Bootstrap also runs before internal-result sync during startup, so an unrelated
internal sync problem cannot prevent historical warm-up. The status API now
shows `bootstrap_target_sports` and the latest `PREDICTIVE_FOOTBALL_STARTUP`
run/error for easier diagnosis.

No betting thresholds, model probabilities, execution rules or live settings
change.


## v0.11.4 — Predictive PostgreSQL LIKE Hotfix

The v0.11.3 runtime finally exposed the exact startup failure:
`tuple index out of range`.

Cause: three PRED1 SQL queries embedded the literal PostgreSQL LIKE pattern
`'soccer_%'` while the database wrapper always calls psycopg2 with a parameter
tuple. psycopg2 treats `%` as part of its parameter interpolation syntax, which
can raise `tuple index out of range` before the historical bootstrap ever
selects a source.

v0.11.4 parameterizes all three football LIKE patterns instead:
- internal football-result sync;
- bootstrap target-league discovery;
- upcoming-fixture forecast discovery.

This is a PostgreSQL compatibility fix only. No model, market, threshold,
execution or live-trading logic changes.


## v0.11.5 — Predictive Execution Polling Fix

The Predictive Football dashboard could show frozen forecasts and derived
market cases while the Execution Funnel remained empty.

Root cause: approved-venue convergence polling only included *existing*
execution-shadow bets. PRED1 needs an approved venue quote in order to create
its first shadow, so this created a circular dependency:

`no shadow -> no convergence poll -> no approved quote -> no shadow`

v0.11.5 makes frozen PRED1 forecasts themselves convergence candidates:
- each frozen score forecast requests approved-venue `h2h` quotes;
- each derived BTTS/totals case requests approved-venue quotes for its market;
- once a qualifying price is seen, the existing first-acceptable-price freeze
  creates the shadow exactly as before;
- once a shadow exists, it continues through the same convergence/CLV path.

No predictive probabilities, 3% entry threshold, 5% strong threshold,
settlement rules or live settings change.


## v0.11.6 — Predictive Result Settlement Coverage

Audit of the first live PRED1 forward sample found a settlement dependency:
the football result collector only requested completed scores for events that
had an open legacy `signals` row. A PRED1-only fixture could therefore produce
forecasts and shadow bets but never receive an `event_results` row unless the
original football engine also happened to signal on the same event.

v0.11.6 changes result eligibility to include either:
- an open original football signal; or
- an unsettled `football_predictive_predictions` row.

The provider result request remains grouped by sport and hourly paced, so this
extends settlement coverage without changing prediction, execution or CLV
rules. Once the score is stored, the normal PRED1 maintenance cycle settles the
forecast Brier/log-loss and all predictive shadow bets.

No database reset. `ENABLE_LIVE_BETTING=false`.

## v0.12.0 — PRED2 Dixon-Coles Challenger

PRED1 remains the frozen control. v0.12.0 adds an isolated **PRED2** forward
challenger using Dixon-Coles low-score dependency correction.

The experiment is deliberately paired: PRED2 uses the same score-only training
history, time decay, attack/defence strengths, forecast horizon, 3%/5% entry
thresholds and approved execution venues as PRED1. It estimates a league-history
`rho` before kickoff, applies Dixon-Coles correction to the score grid, and then
derives 1X2, BTTS and totals probabilities from that corrected grid.

PRED2 has separate prediction/evaluation/bet/price-path tables, separate Brier,
CLV and P&L evidence, and a dedicated `/predictive-football-pred2` dashboard.
`/api/predictive-football/compare` exposes a compact PRED1-v-PRED2 comparison.

PRED2 does **not** add a second paid odds feed. The normal collector merges and
deduplicates PRED1/PRED2 convergence demand at `(event_id, market_key)` before a
provider request. Both models then evaluate the same stored approved-venue wave.

Default controls:

```text
PREDICTIVE_FOOTBALL_PRED2_ENABLED=true
PREDICTIVE_FOOTBALL_PRED2_RHO_MIN=-0.20
PREDICTIVE_FOOTBALL_PRED2_RHO_MAX=0.20
PREDICTIVE_FOOTBALL_PRED2_RHO_STEP=0.01
```

Do not tune PRED2 from its first results. The purpose is a frozen paired forward
comparison against PRED1 on calibration/Brier, A/B CLV and eventually net ROI.


## v0.13.0 — Predictive Evidence Expansion

No PRED1 or PRED2 model probability, rho grid, forecast horizon, edge threshold or
execution acceptance rule is changed. This release spends the larger provider
allowance on **measurement quality** and a separate frozen historical
corroboration cohort.

### 1. Higher-resolution PRED price paths

PRED1/PRED2 approved-venue convergence is now denser as kickoff approaches:
- 12–24h: approximately every 3h;
- 6–12h: every 2h;
- 3–6h: hourly;
- 1–3h: every 30m;
- 30–60m: every 15m;
- 15–30m: every 10m;
- final 15m: every 5m.

PRED1 and PRED2 still share/deduplicate the same `(event_id, market_key)` wave.
The denser path therefore improves A/B-quality CLV without doubling calls just
because two models exist.

### 2. Broad market close capture

For fixtures with a frozen PRED1 or PRED2 forecast, a separate broad **h2h-only**
close lane becomes active inside the final 3 hours. It captures bookmaker
consensus much more densely for closing-market Brier comparison. These waves are
measurement-only: they write `consensus_snapshots` but deliberately do not create
new value/slow-book/cross-market signals.

### 3. Frozen historical PRED1/PRED2 validation

A new isolated historical lane reconstructs past fixtures with the current,
frozen PRED1 and PRED2 specifications. The training cutoff is the original
forecast freeze time (24h before kickoff by default), so later-known scorelines
are excluded even though they already exist in today's database. Historical odds
are sampled at 24h, 6h, 1h and 5m by default.

Stored evidence includes:
- PRED1/PRED2 probabilities, Brier and log loss;
- broad closing-market probabilities and Brier;
- sampled approved-venue entry opportunities at the fixed checkpoints;
- closing price, CLV and commission-adjusted P&L.

The historical cohort is **corroboration, not tuning data**. It never writes to
forward PRED1/PRED2 prediction, evaluation or bet tables, and sampled historical
entries are explicitly not claimed to be exact first-acceptable execution.

New endpoints:
- `/api/predictive-football/historical-validation`
- `/api/predictive-football/historical-bets`
- `POST /admin/predictive-football/historical-run`

New default controls:

```text
DAILY_PAID_CREDIT_BUDGET=1000
MAX_EVENTS_PER_ODDS_CYCLE=3
PREDICTIVE_FOOTBALL_HIGH_RES_PRICE_PATH_ENABLED=true
PREDICTIVE_FOOTBALL_BROAD_CLOSE_ENABLED=true
PREDICTIVE_FOOTBALL_BROAD_CLOSE_HOURS_BEFORE=3
PREDICTIVE_FOOTBALL_HISTORICAL_ENABLED=true
PREDICTIVE_FOOTBALL_HISTORICAL_REGION=uk
PREDICTIVE_FOOTBALL_HISTORICAL_SNAPSHOT_MINUTES=1440,360,60,5
PREDICTIVE_FOOTBALL_HISTORICAL_DAILY_CREDIT_BUDGET=600
PREDICTIVE_FOOTBALL_HISTORICAL_INTERVAL_SECONDS=900
```

The historical events lookup costs one provider credit when it returns events;
each historical h2h event-odds checkpoint costs 10 credits for one region/market.
The default historical ceiling therefore targets roughly 15 fully sampled
fixtures per day at four checkpoints, leaving substantial monthly headroom for
the forward collectors.

No database reset. `ENABLE_LIVE_BETTING=false`.


## v0.13.1 — Predictive Historical PostgreSQL Hotfix

Operational hotfix only. No PRED1/PRED2 probability, threshold, collection cadence,
historical-validation methodology, execution rule or live setting changes.

Fixes a PostgreSQL/psycopg2 dashboard failure introduced in v0.13.0. The historical
summary query embedded the literal SQL wildcard `LIKE 'SKIPPED_%'` while the shared
database wrapper always supplies a parameter tuple. psycopg2 interprets `%` as
parameter-interpolation syntax and raised `IndexError: tuple index out of range`,
causing `/` and `/api/status` to return HTTP 500.

The wildcard is now passed as a bound parameter (`LIKE ?`, `("SKIPPED_%",)`), matching
the established PostgreSQL-safe pattern used elsewhere in Betting Lab. A regression
test prevents reintroduction. No database reset is required.

## v0.14.0 — MS2 Manual Systems Shadow

Adds a separate forward-only Yankee/Heinz research lane while preserving the
existing API-gated MS1 multiples experiment.

MS2 forms:
- **Yankee** cards: 4 selections / 11 component bets;
- **Heinz** cards: 6 selections / 57 component bets.

Default manual-placeable shadow venues are William Hill (`williamhill`) and
Ladbrokes (`ladbrokes_uk`). Betfair Exchange, Matchbook and Smarkets are retained
as **synthetic comparison venues only**; the code does not claim those venues can
accept a Yankee/Heinz ticket.

Source selections must already qualify as open Betting Lab singles. Separate
cohorts are retained for `CONSENSUS`, `PRED1`, `PRED2` and `MIXED_BEST` so later
research can determine whether system-bet economics depend on the source engine.
All legs are from different fixtures.

Every card is capital-normalised: the Yankee/Heinz risks exactly 1u total and is
compared with the same 1u split equally across its constituent singles. The Lab
tracks expected ROI, realised P&L/ROI, drawdown, same-venue CLV and the direct
`system minus singles` result.

A targeted quote-refresh lane prevents fixed-book prices from going stale. It
only activates when at least four qualifying fixtures exist on the same UK date,
uses a 300-credit/day MS2 sub-cap by default, and all of its calls also count in
normal football paid-credit accounting. No order placement is implemented.

New routes:
- `/manual-systems`
- `/api/manual-systems/status`
- `/api/manual-systems/cards`
- `POST /admin/manual-systems/run`

Default new variables:

```text
MANUAL_SYSTEMS_ENABLED=true
MANUAL_SYSTEMS_PLACEABLE_BOOKMAKER_KEYS=williamhill,ladbrokes_uk
MANUAL_SYSTEMS_COMPARISON_BOOKMAKER_KEYS=betfair_ex_uk,matchbook,smarkets
MANUAL_SYSTEMS_TYPES=YANKEE,HEINZ
MANUAL_SYSTEMS_SOURCE_COHORTS=MIXED_BEST,CONSENSUS,PRED1,PRED2
MANUAL_SYSTEMS_QUOTE_FRESHNESS_MINUTES=45
MANUAL_SYSTEMS_MAX_QUOTE_SPREAD_MINUTES=15
MANUAL_SYSTEMS_FORMATION_HORIZON_HOURS=30
MANUAL_SYSTEMS_REFRESH_HORIZON_HOURS=12
MANUAL_SYSTEMS_REFRESH_INTERVAL_MINUTES=30
MANUAL_SYSTEMS_MAX_EVENTS_PER_REFRESH=6
MANUAL_SYSTEMS_DAILY_CREDIT_BUDGET=300
```

`DAILY_PAID_CREDIT_BUDGET` now defaults to `1300` to preserve roughly the prior
1000-credit core allowance plus the new 300-credit MS2 sub-cap. Existing Railway
environment variables still override code defaults.

No database reset. `ENABLE_LIVE_BETTING=false`.


## v0.15.0 — META1 CLV Trust Research

Adds an observation-only research layer designed to answer a different question
from PRED1/PRED2: **which model-value opportunities actually survive to the
closing market?**

META1 freezes entry-time features from every PRED1/PRED2 executable shadow and
labels the row only later when a closing price exists. It has no selection or
execution authority and makes zero provider calls of its own.

Frozen entry-time features include:
- model source (PRED1/PRED2), market, league and bookmaker;
- offered odds, model probability and claimed edge;
- time to kickoff;
- bookmaker count, median/best price, price dispersion and mean overround from
  the already-stored Odds API entry wave;
- PRED1/PRED2 probability agreement and whether both models independently froze
  the same opportunity;
- prediction uncertainty;
- expected total goals, league/training support and match archetype.

Future labels include A/B-quality CLV, whether the entry beat close, closing odds,
result and net P&L. C/STALE closing observations are retained but excluded from
the headline clean-label sample.

Scientific guardrails:
- no odds wave captured after entry can become an entry feature;
- a paired PRED forecast/bet is used only if it already existed at the source
  bet timestamp, so the PRED2 launch catch-up cohort cannot leak future data;
- no meta-model is fitted or given authority from the early sample;
- the first predictive meta-model remains gated until at least 200 clean A/B
  labels by default.

New routes:
- `/meta-edge`
- `/api/meta-edge/status`
- `/api/meta-edge/segments`
- `/api/meta-edge/samples`
- `POST /admin/meta-edge/run`

New variables:

```text
META_EDGE_ENABLED=true
META_EDGE_MIN_CLEAN_LABELS=200
```

META1 uses only existing PRED1/PRED2 and stored Odds API data and therefore adds
**zero paid credits**. A future PRED3 based on xG, shot quality, line-ups,
injuries or tactical/style information will require an additional football-data
source; that is intentionally not part of v0.15.0.

No database reset. `ENABLE_LIVE_BETTING=false`.


## v0.16.0 — PRED3 StatsBomb xG Challenger

Adds **PRED3** as an isolated football forecasting challenger using StatsBomb Open
Data event xG rather than score-only team strength. PRED1 and PRED2 remain frozen
controls.

PRED3 methodology:
- imports selected male domestic StatsBomb Open Data competitions from the public
  GitHub JSON repository;
- sums `shot.statsbomb_xg` for each team in each match and also retains non-penalty
  xG, shot counts and pressure-event counts for later research;
- estimates recency-weighted team xG attack/defence strengths with prior shrinkage;
- converts the resulting expected-goal rates into 1X2, BTTS and totals probabilities;
- freezes forecasts at the same configured 24h horizon as PRED1/PRED2;
- only after freeze does it consult the already-collected approved execution-venue
  price waves;
- skips a fixture when both teams cannot be mapped to enough StatsBomb history or
  when the newest supporting xG evidence exceeds the configured maximum age.

StatsBomb Open Data is selective historical coverage, not a comprehensive live
2026 feed. Sparse PRED3 output is therefore expected and is preferable to filling
coverage gaps with bookmaker information or stale assumptions. The StatsBomb lane
uses free GitHub-hosted JSON and adds **zero Odds API credits**.

New routes:
- `/predictive-football-pred3`
- `/api/predictive-football-pred3/status`
- `/api/predictive-football-pred3/predictions`
- `/api/predictive-football-pred3/bets`
- `/api/predictive-football-pred3/market-bets`
- `POST /admin/predictive-football-pred3/run`

Default variables:

```text
PREDICTIVE_FOOTBALL_PRED3_ENABLED=true
PREDICTIVE_FOOTBALL_PRED3_STATSBOMB_ENABLED=true
PREDICTIVE_FOOTBALL_PRED3_STATSBOMB_BASE_URL=https://raw.githubusercontent.com/hudl/open-data/master/data
PREDICTIVE_FOOTBALL_PRED3_STATSBOMB_REFRESH_SECONDS=86400
PREDICTIVE_FOOTBALL_PRED3_STATSBOMB_MATCHES_PER_CYCLE=12
PREDICTIVE_FOOTBALL_PRED3_STATSBOMB_SEASONS_PER_COMPETITION=4
PREDICTIVE_FOOTBALL_PRED3_MIN_LEAGUE_MATCHES=20
PREDICTIVE_FOOTBALL_PRED3_MIN_TEAM_MATCHES=2.5
PREDICTIVE_FOOTBALL_PRED3_PRIOR_MATCHES=4
PREDICTIVE_FOOTBALL_PRED3_HALF_LIFE_DAYS=365
PREDICTIVE_FOOTBALL_PRED3_LOOKBACK_DAYS=1200
PREDICTIVE_FOOTBALL_PRED3_MAX_DATA_AGE_DAYS=900
```

No database reset. `ENABLE_LIVE_BETTING=false`.

## v0.16.1 — Historical Provider + Bootstrap Hotfix

Rolls forward all v0.16.0 PRED3 functionality and fixes two issues observed in the
2026-09-17 Railway research export.

Historical Odds API:
- historical event discovery now sends only the provider-supported `date`
  snapshot parameter;
- timestamps are normalized to UTC `Z` form;
- kickoff-window filtering is performed locally before fixture matching;
- the existing 24h / 6h / 1h / 5m historical odds checkpoints remain unchanged.

Football-data bootstrap:
- CSV requests now start from the canonical non-`www` host;
- redirects are followed only when they remain HTTPS on football-data.co.uk;
- redirects to `127.0.0.1`, localhost or unrelated hosts are blocked explicitly.

No prediction thresholds, PRED1/PRED2/PRED3 probabilities, execution rules,
MS2/MSP2 rules, META1 logic or live-betting state are changed. No database reset.


## v0.17.0 — Outcome Edge + PXG1 current-performance foundation

This release deliberately **does not change any existing betting or research selection rule**. PRED1, PRED2, PRED3, META1, MS1/MS2, MSP1/MSP2, Tennis, historical validation, price paths and settlement continue exactly as before.

### Outcome Edge research

New page/API:

```text
/outcome-edge
/api/outcome-edge
```

The report de-duplicates settled football selections by event/market/selection/line and centres the evidence on:

- observed win rate
- mean entry-implied win probability
- observed minus implied percentage points
- flat-stake ROI
- A/B-quality CLV as a supporting diagnostic
- sample size and 95% Wilson hit-rate interval in the API payload

This is measurement-only. It cannot filter, create or place a bet.

### PXG1

New page/API:

```text
/proxy-xg
/api/proxy-xg/status
```

PXG1 first reprocesses the already-approved free StatsBomb Open Data matches. For each team-match it extracts ordinary statistics that are also available from a current football-data API (shots, shots on target/off target, blocked shots, shots inside/outside the box, corners and red cards) and learns a fixed ridge-regression mapping to genuine StatsBomb xG.

The model is validated chronologically on a held-out newest 20% sample. The dashboard reports proxy holdout MAE against a constant-xG baseline rather than assuming the proxy is useful.

PXG1 is research-only and has **no betting authority**. It does not feed PRED1/PRED2/PRED3 in v0.17.0.

### Optional current data with API-Football

No paid subscription is required. The build is safe to deploy with no new variable; it will train/backfill the free StatsBomb proxy and show `WAITING FOR KEY` for current data.

To start current completed-match collection, create a free API-Football account and add only:

```text
API_FOOTBALL_KEY=<your key>
```

Defaults are deliberately conservative for the free 100-request/day plan:

```text
PROXY_XG_API_DAILY_CALL_BUDGET=90
PROXY_XG_API_PROVIDER_RESERVE=5
PROXY_XG_API_BACKFILL_DAYS=45
PROXY_XG_API_MATCHES_PER_CYCLE=4
```

The collector reads API-Football's returned quota headers and stops before the configured local/provider reserve. It discovers recent completed fixtures only in competitions already present in Betting Lab.

API keys are sent only in the required `x-apisports-key` request header, are redacted from stored errors, and are excluded from research exports.

### Deployment

Deploy directly over v0.16.1. Do **not** reset Postgres. All database additions are additive.


## v0.18.0 — META2 Frozen CLV Trust Model

v0.18.0 promotes the **research process**, not betting authority. Existing PRED1, PRED2, PRED3, PXG1, Outcome Edge, META1, MS2, MSP1/MSP2, Tennis and historical validation remain unchanged.

When META1 reaches the configured clean A/B-label threshold (default 200), META2 is created once and then frozen:

- clean META1 labels are sorted chronologically;
- the newest 20% (minimum 40 where possible) are reserved as a time-ordered holdout;
- a regularised logistic model estimates `P(beat close)`;
- a regularised linear model estimates expected CLV;
- holdout Brier/CLV-MAE are compared with simple baselines;
- a final shadow model is then fitted to all labels available at the freeze and stored in Postgres;
- future META1 samples are annotated as `FORWARD` without automatic retraining.

META2 has **no selection, rejection, staking or execution authority**. A high META2 score cannot create a bet and a low score cannot suppress one. The purpose is to find out whether entry-time features can identify subsets whose future CLV is materially better than the raw PRED1/PRED2 universe.

New endpoints:

```text
GET /api/meta-edge/model
GET /api/meta-edge/model/scores
```

The `/meta-edge` page now shows META1 collection plus META2 holdout and forward-shadow evidence. Research exports include `meta_edge_model_runs.csv` and `meta_edge_model_scores.csv`.

Deploy directly over v0.17.0. Do **not** reset Postgres. The new tables are additive. No new provider/API calls are introduced. `META_EDGE_MODEL_ENABLED=true` is the default and can be disabled without affecting META1 collection.


## v0.18.1 — PXG1 paid-plan backfill recovery

v0.18.1 includes all v0.18.0 META2 functionality unchanged and fixes a PXG1 recovery edge case discovered from the 2026-09-19 research export. API-Football discovery days that had already exhausted the original three retries while the account was on the Free plan (or during repeated transient timeouts) could remain permanently marked `ERROR` after the account was upgraded.

Discovery-day errors that are plausibly transient now receive delayed retries after a six-hour cooldown, capped at six total attempts. Permanent/non-transient errors remain fail-closed. Existing imported PXG1 rows are not reset, and no betting model or betting threshold is changed.


## v0.19.0 — PRED4 Current PXG + frozen discovery cohorts

v0.19.0 keeps every existing lane and threshold intact while adding two isolated research changes.

### PRED4 Current PXG challenger

- New **PRED4** consumes only completed matches already scored by PXG1.
- It makes **zero provider calls** of its own and has **no live execution authority**.
- A forecast requires at least 20 current league matches, at least 5 raw current matches for each team, at least 3.0 recency-weighted effective matches for each team, and current support no older than 30 days.
- The default recency half-life is 21 days with a 60-day lookback and a 3-match prior.
- The frozen expected-goal rates drive the same 1X2, BTTS and totals markets as the other predictive challengers.
- Bookmaker prices are consulted only after the PRED4 forecast is frozen, using the existing approved execution venues and existing 3% / 5% edge thresholds.
- Existing PRED1/PRED2/PRED3 and PXG1 behaviour is unchanged.

### Frozen forward validation for the interesting cohorts

The current research suggested that PRED1/PRED2 BTTS and selections priced from 4.00–7.49 deserve continued attention. v0.19.0 **does not tune the strategy toward those findings**. Instead it freezes three hypotheses at deployment and keeps all future observations separate from the original discovery sample:

- `PRED12_BTTS`
- `ODDS_4_TO_4_99`
- `ODDS_4_TO_7_49`

The Outcome Edge page/export now reports discovery vs forward-only hit rate, entry-implied probability, flat-stake ROI and A/B CLV for each frozen cohort.

### Outcome Edge coverage fix

Earlier Outcome Edge code attempted to read `market_key` and `point` from PRED h2h tables, which do not contain those columns. The query failure was caught and the PRED lane was silently skipped. v0.19.0 now explicitly reads both the h2h tables and the derived-market tables for PRED1/PRED2/PRED3/PRED4. This is a research-dashboard/export coverage fix only; it does not alter any bet formation or settlement logic.

Deploy directly over v0.18.1. Do not reset Postgres. No new Railway variables are required.
