# Betting Lab v0.14.0 — MS2 Manual Systems Shadow

## Purpose

Adds a new research-only Yankee/Heinz experiment without changing PRED1, PRED2,
football singles, MS1 API-gated multiples, Tennis or Multi-Sport logic.

## MS2 venues

Manual-placeable research venues by default:

- `williamhill`
- `ladbrokes_uk`

Synthetic comparison venues by default:

- `betfair_ex_uk`
- `matchbook`
- `smarkets`

The comparison venues are **not** claimed to accept Yankee/Heinz tickets. They
exist only to answer how the same constituent-leg prices would have behaved.

## Systems

- Yankee: 4 legs / 11 lines
- Heinz: 6 legs / 57 lines

Every card uses different fixtures only. A candidate leg must already be an open,
qualifying Betting Lab single from one of these source cohorts:

- `CONSENSUS`
- `PRED1`
- `PRED2`
- `MIXED_BEST`

For `MIXED_BEST`, only one leg per fixture is retained and the best actual venue
EV is selected from the available source engines.

## Execution realism

MS2 is forward-only. A card forms only when every required leg has a fresh quote
at the same venue, each quote is at or above that source strategy's frozen
minimum acceptable odds, and the quote timestamps fall within the configured
spread window. The first actionable card for a UK date / venue / source cohort /
system type is frozen.

MS2 cannot place an order.

## Equal-capital control

Each system card risks exactly **1.0 unit total**:

- Yankee: `1/11u` on every line
- Heinz: `1/57u` on every line

The control risks the same **1.0 unit total** split equally across the constituent
singles:

- Yankee control: `0.25u` per single
- Heinz control: `1/6u` per single

The dashboard/export therefore compares system ROI, drawdown and P&L with the
same-capital singles basket rather than comparing unequal stakes.

## Targeted quote refresh

To avoid another zero-candidate lane caused by stale fixed-book prices, MS2 has a
small targeted quote collector. It only activates when at least four qualifying
source fixtures occur on the same UK date. It requests only the markets needed
for those candidate fixtures and all configured MS2 venues in the same request.

Defaults:

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

The normal football paid-credit default is raised from 1000 to 1300/day so the
new 300/day MS2 sub-cap does not silently consume the previous core allowance.
If Railway already has an explicit `DAILY_PAID_CREDIT_BUDGET=1000`, change it to
`1300` if you want the intended v0.14.0 headroom.

All `MANUAL_SYSTEM_QUOTES` costs are included in the normal football paid-credit
accounting as well as the MS2 sub-cap.

## New database tables

- `manual_system_shadow_bets`
- `manual_system_shadow_legs`
- `manual_system_shadow_lines`

Migration is additive. **Do not reset Postgres.**

## New UI/API

- `/manual-systems`
- `/api/manual-systems/status`
- `/api/manual-systems/cards`
- `POST /admin/manual-systems/run`

The research export contains all three MS2 tables and a `manual_systems_shadow`
summary.

## Unchanged

- PRED1 Bayesian Poisson specification
- PRED2 Dixon-Coles specification / rho grid
- 24h predictive freeze
- predictive edge thresholds
- singles execution venue rules
- MS1 API-gated multiple execution policy
- live betting remains disabled
