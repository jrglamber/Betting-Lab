from __future__ import annotations

"""Dedicated FHG1 straight-acca research lane built from shared frozen quotes.

FULL uses every prospectively priced FHG1 >=70% selection in one kickoff slate
that is simultaneously compatible with the same accumulator-capable sportsbook.
CORE8 is created only when more than eight compatible legs exist. FHG2 remains
challenger-only. This module makes zero provider calls: it reuses the all-book
quotes already frozen by FHG price capture and retires the redundant legacy
8/9/10 same-book polling loop.
"""

from datetime import datetime, timezone
from html import escape
from math import prod
import json
from typing import Any, Dict, Optional

import first_half_goal_lab as fhg
import first_half_goal_hardening_patch as hard
import first_half_goal_runtime_patch as fhg_runtime
import first_half_goal_curated_patch as curated

VERSION = "FHG_STRAIGHT_ACCA_SHARED_QUOTES_V1"
MODEL_VERSION = "FHG1_ACCA_FORWARD_2026_10_31_V1"
MIN_LEGS = 3
CORE_LEGS = 8
MAX_FULL_LEGS = 30
MAX_QUOTE_SPREAD_MINUTES = 20.0

_INSTALLED = False
_ORIGINAL_MAINTENANCE = None
_ORIGINAL_SCOREBOARD = None
_ORIGINAL_DASHBOARD = None
_ORIGINAL_CURATED_SECTION = None
_ORIGINAL_LEGACY_CAPTURE = None


def _utc(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _ensure_schema(db) -> None:
    iid = "BIGSERIAL PRIMARY KEY" if getattr(db, "is_postgres", False) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute(f"""CREATE TABLE IF NOT EXISTS first_half_goal_acca_cards(
        id {iid}, slate_key TEXT NOT NULL, variant TEXT NOT NULL,
        model_version TEXT NOT NULL, created_at TEXT NOT NULL,
        kickoff_time TEXT NOT NULL, bookmaker_key TEXT NOT NULL,
        bookmaker_title TEXT, leg_count INTEGER NOT NULL,
        combined_odds REAL NOT NULL, joint_model_probability REAL NOT NULL,
        expected_value_pct REAL, minimum_probability REAL NOT NULL,
        quote_spread_minutes REAL NOT NULL, event_ids_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN', settled_legs INTEGER NOT NULL DEFAULT 0,
        hit_legs INTEGER NOT NULL DEFAULT 0, all_hit INTEGER,
        return_units REAL, pnl_units REAL, roi_pct REAL, settled_at TEXT,
        UNIQUE(slate_key,variant,model_version)
    )""")
    try:
        db.execute("CREATE INDEX IF NOT EXISTS idx_fhg_acca_status ON first_half_goal_acca_cards(status,kickoff_time)")
    except Exception:
        pass


def _book_map(db, candidate_id: int) -> dict[str, dict[str, Any]]:
    try:
        rows = db.fetchall(
            """SELECT bookmaker_key,bookmaker_title,entry_odds,captured_at
               FROM first_half_goal_bookmaker_quotes
               WHERE candidate_id=? ORDER BY captured_at,bookmaker_key""",
            (candidate_id,),
        )
    except Exception:
        rows = []
    out: dict[str, dict[str, Any]] = {}
    exchange = {"betfair_ex_uk", "matchbook", "smarkets"}
    for raw in rows:
        row = dict(raw)
        key = str(row.get("bookmaker_key") or "")
        if not key or key.lower() in exchange:
            continue
        try:
            if float(row.get("entry_odds") or 0.0) <= 1.0:
                continue
        except Exception:
            continue
        out[key] = row
    return out


def _insert_variant(db, slate: str, variant: str, candidates: list[dict[str, Any]], now: datetime) -> bool:
    if len(candidates) < MIN_LEGS:
        return False
    common: Optional[set[str]] = None
    maps: dict[int, dict[str, dict[str, Any]]] = {}
    for c in candidates:
        bm = _book_map(db, int(c["id"]))
        maps[int(c["id"])] = bm
        keys = set(bm)
        common = keys if common is None else common & keys
    common = common or set()
    if not common:
        return False

    ranked: list[tuple[float, float, str]] = []
    for book in common:
        times = []
        odds = []
        valid = True
        for c in candidates:
            q = maps[int(c["id"])][book]
            dt = fhg._dt(q.get("captured_at"))
            if dt is None:
                valid = False
                break
            times.append(dt)
            odds.append(float(q["entry_odds"]))
        if not valid:
            continue
        spread = (max(times) - min(times)).total_seconds() / 60.0
        if spread <= MAX_QUOTE_SPREAD_MINUTES:
            ranked.append((prod(odds), spread, book))
    if not ranked:
        return False

    combined, spread, best_book = max(ranked, key=lambda x: x[0])
    sample = maps[int(candidates[0]["id"])][best_book]
    joint = prod(float(c.get("estimated_probability") or 0.0) for c in candidates)
    ev_pct = (joint * combined - 1.0) * 100.0
    changed = db.execute(
        """INSERT INTO first_half_goal_acca_cards(
               slate_key,variant,model_version,created_at,kickoff_time,bookmaker_key,
               bookmaker_title,leg_count,combined_odds,joint_model_probability,
               expected_value_pct,minimum_probability,quote_spread_minutes,event_ids_json,status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(slate_key,variant,model_version) DO NOTHING""",
        (
            slate, variant, MODEL_VERSION, now.isoformat(), str(candidates[0]["commence_time"]),
            best_book, str(sample.get("bookmaker_title") or best_book), len(candidates),
            float(combined), float(joint), float(ev_pct),
            min(float(c.get("estimated_probability") or 0.0) for c in candidates),
            float(spread), json.dumps([str(c["event_id"]) for c in candidates]), "OPEN",
        ),
    )
    return int(changed or 0) > 0


def create_accas(db, now: Optional[datetime] = None) -> Dict[str, int]:
    _ensure_schema(db)
    now = _utc(now)
    try:
        slates = db.fetchall(
            """SELECT DISTINCT slate_key FROM first_half_goal_candidates
               WHERE status='OPEN' AND model_version=? AND estimated_probability>=?
                 AND commence_time>? ORDER BY slate_key""",
            (fhg.MODEL_VERSION, float(fhg.THRESHOLD), now.isoformat()),
        )
    except Exception:
        slates = []
    full = core = 0
    for row in slates:
        slate = str(row.get("slate_key") or "")
        try:
            cs = [dict(x) for x in db.fetchall(
                """SELECT c.* FROM first_half_goal_candidates c
                   WHERE c.status='OPEN' AND c.slate_key=? AND c.model_version=?
                     AND c.estimated_probability>=? AND c.commence_time>?
                     AND EXISTS(SELECT 1 FROM first_half_goal_bookmaker_quotes q WHERE q.candidate_id=c.id)
                   ORDER BY c.estimated_probability DESC,c.event_id LIMIT ?""",
                (slate, fhg.MODEL_VERSION, float(fhg.THRESHOLD), now.isoformat(), MAX_FULL_LEGS),
            )]
        except Exception:
            cs = []
        if len(cs) < MIN_LEGS:
            continue
        if _insert_variant(db, slate, "FULL", cs, now):
            full += 1
        if len(cs) > CORE_LEGS and _insert_variant(db, slate, "CORE8", cs[:CORE_LEGS], now):
            core += 1
    return {"full_created": full, "core8_created": core}


def settle_accas(db, now: Optional[datetime] = None) -> int:
    _ensure_schema(db)
    now = _utc(now)
    terminal = {"SUPERSEDED", "VOID", "POSTPONED", "CANCELLED", "CANCELED", "ABANDONED"}
    n = 0
    for card in db.fetchall("SELECT * FROM first_half_goal_acca_cards WHERE status='OPEN'"):
        try:
            ids = list(json.loads(card.get("event_ids_json") or "[]"))
        except Exception:
            ids = []
        rows = [db.fetchone("SELECT status,fhg_hit FROM first_half_goal_candidates WHERE event_id=?", (eid,)) for eid in ids]
        statuses = {str((r or {}).get("status") or "").upper() for r in rows}
        if statuses & terminal:
            decided = [r for r in rows if r and str(r.get("status") or "") == "SETTLED" and r.get("fhg_hit") is not None]
            hits = sum(int(r.get("fhg_hit") or 0) for r in decided)
            db.execute(
                """UPDATE first_half_goal_acca_cards SET status='VOID',settled_legs=?,hit_legs=?,
                   all_hit=NULL,return_units=NULL,pnl_units=NULL,roi_pct=NULL,settled_at=?
                   WHERE id=? AND status='OPEN'""",
                (len(decided), hits, now.isoformat(), card["id"]),
            )
            n += 1
            continue
        if not rows or any(not r or str(r.get("status") or "") != "SETTLED" or r.get("fhg_hit") is None for r in rows):
            continue
        hits = sum(int(r.get("fhg_hit") or 0) for r in rows)
        won = hits == len(rows)
        ret = float(card["combined_odds"]) if won else 0.0
        pnl = ret - 1.0
        db.execute(
            """UPDATE first_half_goal_acca_cards SET status='SETTLED',settled_legs=?,hit_legs=?,all_hit=?,
               return_units=?,pnl_units=?,roi_pct=?,settled_at=? WHERE id=? AND status='OPEN'""",
            (len(rows), hits, 1 if won else 0, ret, pnl, pnl * 100.0, now.isoformat(), card["id"]),
        )
        n += 1
    return n


def _metrics(db) -> Dict[str, Any]:
    _ensure_schema(db)
    rows = [dict(r) for r in db.fetchall("SELECT * FROM first_half_goal_acca_cards ORDER BY created_at DESC,id DESC")]
    settled = [r for r in rows if str(r.get("status") or "") == "SETTLED" and r.get("pnl_units") is not None]
    pnl = sum(float(r.get("pnl_units") or 0.0) for r in settled)
    return {
        "cards": len(rows), "open": sum(1 for r in rows if str(r.get("status") or "") == "OPEN"),
        "settled": len(settled), "wins": sum(int(r.get("all_hit") or 0) for r in settled),
        "pnl_units": pnl, "roi_pct": pnl / len(settled) * 100.0 if settled else None,
        "rows": rows,
    }


def _panel(db) -> str:
    m = _metrics(db)
    trs = []
    for r in m["rows"][:20]:
        status = str(r.get("status") or "OPEN")
        if status == "SETTLED":
            status = "WON" if int(r.get("all_hit") or 0) else "LOST"
        pnl = r.get("pnl_units")
        trs.append(
            "<tr>" +
            f"<td>{escape(str(r.get('variant') or ''))}</td><td>{int(r.get('leg_count') or 0)}</td>" +
            f"<td>{escape(str(r.get('bookmaker_title') or r.get('bookmaker_key') or '—'))}</td>" +
            f"<td>{float(r.get('combined_odds') or 0.0):.2f}</td>" +
            f"<td>{float(r.get('joint_model_probability') or 0.0)*100:.1f}%</td>" +
            f"<td>{float(r.get('quote_spread_minutes') or 0.0):.0f}m</td><td>{escape(status)}</td>" +
            f"<td>{'—' if pnl is None else f'{float(pnl):+.2f}u'}</td></tr>"
        )
    if not trs:
        trs = ["<tr><td colspan='8'>No same-book FHG acca has formed yet.</td></tr>"]
    roi = "—" if m["roi_pct"] is None else f"{float(m['roi_pct']):+.1f}%"
    return (
        "<div class='panel'><h2>FHG straight acca <span class='pill'>FHG1 ≥70%</span></h2>"
        "<div class='summary'><strong>Straight accumulator only.</strong> FULL uses every priced qualifying "
        "leg in the kickoff slate; CORE8 is created only when more than eight are available. Quotes must "
        f"share one sportsbook and be frozen within {int(MAX_QUOTE_SPREAD_MINUTES)} minutes. No doubles, "
        "Trixies, Yankees or Heinz are created here. FHG2 remains challenger-only.</div>"
        f"<div class='muted'>Cards {m['cards']} · open {m['open']} · settled {m['settled']} · wins {m['wins']} · "
        f"P/L {float(m['pnl_units']):+.2f}u · ROI {roi}</div>"
        "<div class='tablewrap'><table><thead><tr><th>Version</th><th>Legs</th><th>Book</th><th>Acca odds</th>"
        "<th>Model joint</th><th>Quote spread</th><th>Status</th><th>P/L</th></tr></thead><tbody>" +
        "".join(trs) + "</tbody></table></div></div>"
    )


def _export_tables() -> None:
    fhg.TABLES = tuple(dict.fromkeys(tuple(fhg.TABLES) + ("first_half_goal_acca_cards",)))
    try:
        import exporter
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + ("first_half_goal_acca_cards",)))
    except Exception:
        pass
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + ("first_half_goal_acca_cards",)))
        split_exporter.MANUAL_TABLES = tuple(dict.fromkeys(tuple(split_exporter.MANUAL_TABLES) + ("first_half_goal_acca_cards",)))
    except Exception:
        pass


def install(base_app_module) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_MAINTENANCE, _ORIGINAL_SCOREBOARD
    global _ORIGINAL_DASHBOARD, _ORIGINAL_CURATED_SECTION, _ORIGINAL_LEGACY_CAPTURE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    db = base_app_module.db
    _ensure_schema(db)

    _ORIGINAL_LEGACY_CAPTURE = hard.capture_same_book_cards
    def _legacy_disabled(_web, _db, now=None):
        return {"ok": True, "attempted_slates": 0, "polled_events": 0, "placeable_cards": 0,
                "disabled": True, "reason": "replaced_by_fhg_straight_acca", "betting_authority": False}
    hard.capture_same_book_cards = _legacy_disabled

    _ORIGINAL_MAINTENANCE = fhg.run_maintenance
    def maintenance_with_acca(db_obj, now=None):
        state = dict(_ORIGINAL_MAINTENANCE(db_obj, now))
        state["fhg_acca_created"] = create_accas(db_obj, now)
        state["fhg_acca_settled"] = settle_accas(db_obj, now)
        state["fhg_acca_version"] = VERSION
        state["betting_authority"] = False
        return state
    fhg.run_maintenance = maintenance_with_acca
    fhg_runtime.run_maintenance = maintenance_with_acca

    _ORIGINAL_SCOREBOARD = fhg.scoreboard
    def scoreboard_with_acca(db_obj):
        out = dict(_ORIGINAL_SCOREBOARD(db_obj))
        m = _metrics(db_obj)
        out["straight_acca"] = {k: v for k, v in m.items() if k != "rows"}
        return out
    fhg.scoreboard = scoreboard_with_acca
    curated.scoreboard = scoreboard_with_acca

    _ORIGINAL_DASHBOARD = fhg._dashboard
    def dashboard_with_acca(db_obj, style):
        html = _ORIGINAL_DASHBOARD(db_obj, style)
        return html.replace("</body>", _panel(db_obj) + "</body>", 1) if "</body>" in html else html + _panel(db_obj)
    fhg._dashboard = dashboard_with_acca

    _ORIGINAL_CURATED_SECTION = curated._section
    def curated_with_acca(db_obj):
        html = _ORIGINAL_CURATED_SECTION(db_obj)
        return html.replace("</section>", _panel(db_obj) + "</section>", 1) if "</section>" in html else html + _panel(db_obj)
    curated._section = curated_with_acca

    _export_tables()
    startup = {"created": create_accas(db), "settled": settle_accas(db)}
    _INSTALLED = True
    return {
        "installed": True, "already_installed": False, "version": VERSION,
        "model_version": MODEL_VERSION, "fhg1_threshold": float(fhg.THRESHOLD),
        "straight_acca_only": True, "full_acca": True, "core8_when_gt8": True,
        "minimum_legs": MIN_LEGS, "max_full_legs_safety_cap": MAX_FULL_LEGS,
        "shared_quote_reuse": True, "new_provider_calls": 0,
        "legacy_same_book_8_9_10_polling": False, "fhg2_acca_authority": False,
        "betting_authority": False, "startup": startup,
    }


__all__ = ["VERSION", "MODEL_VERSION", "install", "create_accas", "settle_accas"]
