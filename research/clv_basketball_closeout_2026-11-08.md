# CLV-first basketball research closeout — 2026-11-08

## Source
Uploaded weekly research export: `betting-lab-research-weekly-20261108T223939Z.zip`. Offline analysis of `tables/clv_first_samples.csv` using the research replay grouping rules. These are **not** live execution returns.

## Forward basketball CLV observations
- ACCEPT: 240 records; 236 settled; source ledger +17.7357 units before replay commission assumptions.
- OBSERVE: 32 records; 29 settled; source ledger -13.035 units.
- Historical records and non-basketball sports excluded.

## ACCEPT replay, 1-unit total stake per card
| Strategy | Settled cards | P&L (units) | ROI |
| --- | ---: | ---: | ---: |
| Singles | 236 | +16.5045 | +6.99% |
| Doubles | 85 | +1.6783 | +1.97% |
| Trebles | 38 | -17.4721 | -45.98% |
| Yankees | 18 | -9.2728 | -51.52% |

Doubles by exchange: Betfair Exchange UK 58 settled / +17.731u; Matchbook 19 / -8.053u; Smarkets 8 / -8u.

Doubles by ISO week (card formation): 2026-W42 10 / +1.105u; W43 36 / -3.824u; W44 28 / +10.066u; W45 11 / -5.668u.

OBSERVE replay: singles 29 settled / -13.035u; doubles 7 / -7u; trebles 2 / -2u; Yankees 1 / -1u.

## Interpretation and limitations
1. **No live promotion.** Doubles have only +1.97% modelled ROI, negative results in two of four observed weeks, and large cross-exchange variation. Trebles and Yankees are materially negative.
2. Singles remain an exploratory shadow signal. Their +6.99% replay ROI does not establish statistical significance, available liquidity, price availability or real executable profit.
3. The replay applies an assumed 5% commission on positive card returns, not venue-specific commission or settlement-ledger exact reconciliation. Recorded exchange odds may not be executable at the time of formation.
4. The grouping is greedy and non-overlapping, not a full production-grade chronological queue simulator. Event correlation, competing selection opportunities and stake limits still require separate validation.
5. The latest code CI passed (GitHub Actions run #9 for commit `326e8591fe265026186fe38495affb89547fbdd0`); that validates unit tests, not betting profitability.

## Final decision
**Close this development iteration as research-only.** Keep PR #42 draft/unmerged; no changes to HR2 live policies, Telegram, bankroll or stakes. Revisit only after a larger prospective sample and executable price/commission reconciliation. No further feature work is justified by these data.
