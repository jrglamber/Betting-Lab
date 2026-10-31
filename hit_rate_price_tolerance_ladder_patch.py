from __future__ import annotations

"""Production wrapper for the forward HR price-tolerance experiment.

Keeps the original HR_PRICE_TOLERANCE_FORWARD_V1 evidence and installation
clock intact, but expands the tolerated-price ladder and surfaces every active
HR system. This is research-only: it reuses the canonical HR quote cache,
changes no source selections, consumes no canonical candidates, makes no
provider calls, sends no alerts and has no betting authority.
"""

from html import escape
from typing import Any, Dict

import hit_rate_multiples_lab as hr
import hit_rate_price_tolerance_patch as pt
from db import utc_now_iso

VERSION = "HR_PRICE_TOLERANCE_LADDER_2026_10_31_V2"
VARIANTS = {
    "P100": 1.00,
    "P98": 0.98,
    "P96": 0.96,
    "P94": 0.94,
    "P90": 0.90,
    "P85": 0.85,
}

_INSTALLED = False
_ORIGINAL_SCOREBOARD = pt.scoreboard


def _systems_for(exp: str):
    if exp == "HR1":
        return tuple(x for x in ("YANKEE", "HEINZ") if x in hr.SYSTEMS)
    return tuple(x for x in ("YANKEE", "TRIXIE", "DOUBLE", "HEINZ") if x in hr.SYSTEMS)


def _comparison_start(db) -> str:
    db.execute("""CREATE TABLE IF NOT EXISTS hit_rate_price_tolerance_ladder_meta (
        version TEXT PRIMARY KEY,
        installed_at TEXT NOT NULL
    )""")
    row = db.fetchone(
        "SELECT installed_at FROM hit_rate_price_tolerance_ladder_meta WHERE version=?",
        (VERSION,),
    )
    if not row:
        stamp = utc_now_iso()
        db.execute(
            """INSERT INTO hit_rate_price_tolerance_ladder_meta(version,installed_at)
               VALUES(?,?) ON CONFLICT(version) DO NOTHING""",
            (VERSION, stamp),
        )
        row = db.fetchone(
            "SELECT installed_at FROM hit_rate_price_tolerance_ladder_meta WHERE version=?",
            (VERSION,),
        ) or {"installed_at": stamp}
    return str(row.get("installed_at") or utc_now_iso())


def _install_exports() -> None:
    table = "hit_rate_price_tolerance_ladder_meta"
    try:
        import exporter
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + (table,)))
    except Exception:
        pass
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + (table,)))
        split_exporter.MANUAL_TABLES = tuple(dict.fromkeys(tuple(split_exporter.MANUAL_TABLES) + (table,)))
    except Exception:
        pass


def _metrics_since(db, variant: str, exp: str, system: str, start_at: str) -> Dict[str, Any]:
    rows = [dict(x) for x in db.fetchall(
        """SELECT * FROM hit_rate_price_tolerance_cards
           WHERE variant=? AND experiment_key=? AND system_type=? AND created_at>=?
           ORDER BY id""",
        (variant, exp, system, start_at),
    )]
    done = [x for x in rows if x.get("pnl_units") is not None]
    pnl = sum(float(x.get("pnl_units") or 0) for x in done)
    return {
        "cards": len(rows),
        "settled": len(done),
        "profitable": sum(float(x.get("pnl_units") or 0) > 1e-12 for x in done),
        "pnl_units": pnl,
        "roi_pct": (pnl / len(done) * 100.0) if done else None,
        "full_house": sum(
            int(x.get("winning_legs") or 0) == int(hr.SYSTEMS[system]["legs"]) for x in done
        ),
    }


def _scoreboard(db) -> Dict[str, Any]:
    # Keep the old P98/P96/P94 evidence accessible, but compare all six variants
    # only from the common ladder activation time so counts and ROI are fair.
    all_time = _ORIGINAL_SCOREBOARD(db)
    start_at = _comparison_start(db)
    comparable = {
        variant: {
            "tolerance": tolerance,
            "streams": {
                exp: {
                    system: _metrics_since(db, variant, exp, system, start_at)
                    for system in hr.SYSTEMS
                }
                for exp in ("HR1", "HR2")
            },
        }
        for variant, tolerance in VARIANTS.items()
    }
    return {
        "version": VERSION,
        "underlying_version": pt.VERSION,
        "forward_start_at": all_time.get("forward_start_at"),
        "comparison_start_at": start_at,
        "provider_calls_added": 0,
        "same_book_required": True,
        "canonical_hr_state_changed": False,
        "variants": comparable,
        "all_time_prior_evidence": all_time.get("variants", {}),
    }


def _page(web_module) -> str:
    score = _scoreboard(web_module.db)
    rows = []
    for variant, payload in score["variants"].items():
        tol = float(payload["tolerance"]) * 100.0
        for exp in ("HR1", "HR2"):
            for system in _systems_for(exp):
                m = payload["streams"][exp][system]
                roi = "—" if m["roi_pct"] is None else f"{float(m['roi_pct']):+.1f}%"
                rows.append(
                    f"<tr><td><strong>{escape(variant)}</strong><div class='muted'>≥{tol:.0f}% of frozen minimum price</div></td>"
                    f"<td>{escape(exp)}</td><td>{escape(system.title())}</td><td>{m['cards']}</td>"
                    f"<td>{m['settled']}</td><td>{m['profitable']}</td><td>{m['full_house']}</td>"
                    f"<td>{float(m['pnl_units']):+.2f}u</td><td>{roi}</td></tr>"
                )
    body = "".join(rows) or "<tr><td colspan='9'>No forward tolerance cards yet.</td></tr>"
    variants = " / ".join(VARIANTS)
    return f"""<!doctype html><html><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>HR price tolerance</title><style>{web_module.BASE_STYLE}</style></head><body>
    <a href='/'>← Betting Lab</a> · <a href='/hit-rate-multiples'>Hit-Rate Multiples</a>
    <h1>HR Price Tolerance <span class='pill'>FORWARD SHADOW</span></h1>
    <div class='sub'>Same-book remains mandatory. {escape(variants)} reuse the existing HR quote cache and make
    <strong>zero additional provider calls</strong>. P100 is the frozen minimum-price baseline; lower variants
    accept progressively less of that price so we can measure extra card frequency against price sacrifice,
    P&amp;L and ROI. Source pockets, canonical HR cards and canonical HR P&amp;L are untouched.<br>
    Fair-comparison window starts: <strong>{escape(str(score['comparison_start_at']))}</strong>.
    Older P98/P96/P94 evidence remains preserved in the database/export but is excluded from this head-to-head table.</div>
    <div class='panel'><table><thead><tr><th>Variant</th><th>Pool</th><th>System</th><th>Cards</th>
    <th>Settled</th><th>Profitable</th><th>All legs won</th><th>P&amp;L</th><th>ROI</th></tr></thead>
    <tbody>{body}</tbody></table></div></body></html>"""


def install(db=None) -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "variants": VARIANTS,
        }

    # Preserve pt.VERSION so the original forward-start timestamp and all
    # P98/P96/P94 evidence remain continuous. Only the active variant ladder is
    # expanded prospectively from this deployment.
    pt.VARIANTS = dict(VARIANTS)
    pt.scoreboard = _scoreboard
    pt._page = _page
    comparison_start_at = _comparison_start(db)
    _install_exports()
    result = dict(pt.install(db) or {})

    # Seed/settle once at startup from already-collected fresh quotes. This adds
    # no provider calls and makes the restored lane observable immediately.
    try:
        maintenance = pt.run_maintenance(db) if db is not None else None
    except Exception as exc:
        maintenance = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "underlying_version": pt.VERSION,
        "variants": VARIANTS,
        "systems": {
            "HR1": list(_systems_for("HR1")),
            "HR2": list(_systems_for("HR2")),
        },
        "same_book_required": True,
        "provider_calls_added": 0,
        "betting_authority": False,
        "canonical_hr_state_changed": False,
        "forward_start_preserved": True,
        "comparison_start_at": comparison_start_at,
        "prior_p98_p96_p94_evidence_preserved": True,
        "comparison_meta_exported": True,
        "maintenance": maintenance,
        "underlying_install": result,
        "dashboard": "/hit-rate-price-tolerance",
    }
