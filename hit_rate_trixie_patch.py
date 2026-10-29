from __future__ import annotations

"""Forward-only HR2 Trixie + Double challengers.

This patch adds independent smaller-system streams to the existing HR2
hit-rate multiples lab without changing the frozen Yankee or Heinz evidence.

Rules:
- HR2 only. HR1 remains Yankee + Heinz.
- Trixie: 3 different events; 3 doubles + 1 treble.
- Double: 2 different events; one double.
- £10 total reference stake per card; the existing generic settlement engine
  splits that total equally across each system's lines.
- Uses the existing fresh-price and same-book-only formation policy.
- Forward only from deployment: no historical cards are reconstructed/backfilled.
- Research only. Existing Telegram wiring intentionally remains limited to
  Yankee/Heinz, so neither challenger is sent as a bet-ready alert.
"""

from typing import Any, Dict

import hit_rate_multiples_lab as hr
from db import utc_now_iso

VERSION = "HIT_RATE_HR2_SMALL_SYSTEMS_FORWARD_V2"
EXPERIMENT = "HR2"
SYSTEM_SPECS = {
    "TRIXIE": {"legs": 3, "sizes": (2, 3)},
    "DOUBLE": {"legs": 2, "sizes": (2,)},
}
SMALL_SYSTEMS = tuple(SYSTEM_SPECS)

_INSTALLED = False
_ORIGINAL_INGEST = None
_ORIGINAL_SCOREBOARD = None
_ORIGINAL_PAGE = None


def _ingest_hr2_small_systems_only(db, now) -> Dict[str, int]:
    """Mirror HR ingestion while restricting Trixie/Double candidates to HR2."""
    seen = inserted = 0
    for row in hr._source_rows(db, now):
        exp = str(row.get("experiment_key") or "")
        systems = tuple(hr.SYSTEMS)
        if exp != EXPERIMENT:
            systems = tuple(system for system in systems if system not in SMALL_SYSTEMS)

        for system in systems:
            seen += 1
            stamp = utc_now_iso()
            key = hr._key(hr.POLICY_VERSION, exp, system, row)
            try:
                inserted += int(
                    db.execute(
                        """INSERT INTO hit_rate_candidates(
                            candidate_key,policy_version,experiment_key,system_type,first_seen_at,source_created_at,
                            source_lane,source_table,source_id,event_id,sport_key,league,market_key,selection,point,
                            min_odds,source_odds,band_min,band_max,commence_time,home_team,away_team,status
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(candidate_key) DO NOTHING""",
                        (
                            key,
                            hr.POLICY_VERSION,
                            exp,
                            system,
                            stamp,
                            str(row.get("source_created_at") or stamp),
                            row["source_lane"],
                            row["source_table"],
                            int(row.get("source_id") or 0),
                            str(row.get("event_id") or ""),
                            str(row.get("sport_key") or ""),
                            str(row.get("league") or ""),
                            str(row.get("market_key") or "h2h"),
                            str(row.get("selection") or ""),
                            row.get("point"),
                            float(row.get("min_odds") or 0),
                            float(row.get("source_odds") or 0),
                            float(row["band_min"]),
                            float(row["band_max"]),
                            str(row.get("commence_time") or ""),
                            str(row.get("home_team") or ""),
                            str(row.get("away_team") or ""),
                            "QUEUED",
                        ),
                    )
                    > 0
                )
            except Exception:
                pass
    return {"source_stream_rows": seen, "inserted": inserted}


def _scoreboard(db) -> Dict[str, Any]:
    """Keep HR1 visually unchanged while surfacing the HR2 challengers."""
    out = _ORIGINAL_SCOREBOARD(db)
    streams = out.get("streams") or {}
    hr1 = streams.get("HR1") or {}
    for system in SMALL_SYSTEMS:
        hr1.pop(system, None)
    out["small_systems_forward"] = {
        "experiment": EXPERIMENT,
        "research_only": True,
        "forward_only": True,
        "historical_backfill": False,
        "telegram_bet_ready": False,
        "systems": {
            "TRIXIE": {"legs": 3, "lines": 4},
            "DOUBLE": {"legs": 2, "lines": 1},
        },
        "reference_stake_gbp": float(hr.REFERENCE_STAKE_GBP),
    }
    return out


def _stream_row(metrics: Dict[str, Any], system: str) -> str:
    roi = "—" if metrics["roi_pct"] is None else f"{float(metrics['roi_pct']):+.1f}%"
    return (
        f"<tr><td>{EXPERIMENT}</td><td>{system.title()}</td>"
        f"<td>{metrics['queued']}</td><td>{metrics['queued_events']}</td>"
        f"<td>{metrics['cards']}</td><td>{metrics['settled']}</td>"
        f"<td>{metrics['profitable']}</td><td>{metrics['at_least_3']}</td>"
        f"<td>{metrics['full_house']}</td><td>{roi}</td></tr>"
    )


def _page(web_module) -> str:
    """Add the HR2 Trixie and Double challengers to the HR dashboard."""
    html = _ORIGINAL_PAGE(web_module)
    try:
        score = hr.scoreboard(web_module.db)
        rows = []
        for system in ("TRIXIE", "DOUBLE"):
            metrics = score["streams"][EXPERIMENT][system]
            rows.append(_stream_row(metrics, system))

        marker = "</tbody></table></div></div>\n<div class='panel'><h2>Assembly</h2>"
        if marker in html and "<td>HR2</td><td>Trixie</td>" not in html:
            html = html.replace(
                marker,
                "".join(rows) + "</tbody></table></div></div>\n<div class='panel'><h2>Assembly</h2>",
                1,
            )

        panel_marker = "<div class='panel'><h2>Frozen design backtest</h2>"
        if panel_marker in html and "HR2 small-system challengers" not in html:
            panel = (
                "<div class='panel'><h2>HR2 small-system challengers "
                "<span class='pill'>RESEARCH ONLY</span></h2>"
                "<div class='summary'><strong>Trixie: 3 legs / 4 lines. Double: 2 legs / 1 line. "
                "£10 total reference stake per card.</strong> Both use the same HR2 candidate pool, "
                "fresh-price thresholds and same-book-only assembly as the existing HR2 multiples. "
                "Each forms independently as soon as enough qualifying selections are simultaneously "
                "placeable at one bookmaker.</div>"
                "<div class='muted'>Forward evidence starts from deployment. No historical Trixie or "
                "Double cards are backfilled, existing Yankee/Heinz cards are untouched, and neither "
                "challenger is sent as a Telegram bet-ready alert. In the main stream table, the legacy "
                "'3+ wins' column is not meaningful for Double; use 'All legs' and ROI for that lane.</div></div>"
            )
            html = html.replace(panel_marker, panel + panel_marker, 1)

        html = html.replace(
            "High-hit-rate/value pockets, assembled chronologically. Yankees are primary; Heinz runs independently as a benchmark.",
            "High-hit-rate/value pockets, assembled chronologically. Yankees remain primary; HR2 Trixie and Double are forward-only smaller-system challengers and Heinz remains an independent benchmark.",
            1,
        )
        html = html.replace(
            "Yankee and Heinz are independent forward experiments.",
            "Yankee, HR2 Trixie, HR2 Double and Heinz are independent forward experiments.",
            1,
        )
    except Exception:
        # Presentation must never interfere with collection/settlement.
        pass
    return html


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_INGEST, _ORIGINAL_SCOREBOARD, _ORIGINAL_PAGE

    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "experiment": EXPERIMENT,
            "systems": list(SMALL_SYSTEMS),
        }

    # Add both systems before the later same-book-only runtime patch installs.
    # Its generic formation logic reads hr.SYSTEMS dynamically, so both streams
    # inherit the same placeability policy without a parallel odds collector.
    for system, spec in SYSTEM_SPECS.items():
        hr.SYSTEMS[system] = dict(spec)

    _ORIGINAL_INGEST = hr._ingest
    _ORIGINAL_SCOREBOARD = hr.scoreboard
    _ORIGINAL_PAGE = hr._page

    hr._ingest = _ingest_hr2_small_systems_only
    hr.scoreboard = _scoreboard
    hr._page = _page

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "experiment": EXPERIMENT,
        "systems": {
            system: {
                "legs": int(spec["legs"]),
                "line_sizes": list(spec["sizes"]),
                "line_count": 4 if system == "TRIXIE" else 1,
            }
            for system, spec in SYSTEM_SPECS.items()
        },
        "reference_stake_gbp": float(hr.REFERENCE_STAKE_GBP),
        "forward_only": True,
        "historical_backfill": False,
        "telegram_bet_ready": False,
        "separate_provider_polling_loop": False,
        "quote_cache": "shared_existing_hr_event_quotes",
        "existing_yankee_heinz_evidence_changed": False,
    }
