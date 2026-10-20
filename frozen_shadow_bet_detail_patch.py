from __future__ import annotations

"""Per-bet audit trail for the frozen singles forward portfolio.

Presentation/reporting only. Reconstructs the same frozen lane boundaries,
reference stakes and cross-lane deduplication used by the portfolio headline,
then exposes every qualifying forward shadow bet in a collapsible dashboard table.
"""

from datetime import datetime, timezone
from html import escape
from typing import Any, Dict, Mapping, Sequence
from zoneinfo import ZoneInfo

import dashboard_layout_v2
import frozen_shadow_dashboard_patch as dashboard
import frozen_shadow_portfolio_patch as portfolio

VERSION = "FROZEN_SHADOW_BET_DETAIL_V1"
DASHBOARD_VERSION = "0.19.77"
LONDON = ZoneInfo("Europe/London")

_INSTALLED = False
_ORIGINAL_SECTION = None


def _query(db, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    try:
        return [dict(r) for r in db.fetchall(sql, tuple(params))]
    except Exception:
        return []


def _point_key(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.4f}"
    except Exception:
        return str(value)


def _selection_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("event_id") or ""),
        str(row.get("market_key") or ""),
        str(row.get("selection") or ""),
        _point_key(row.get("point")),
    )


def _dedupe_best_price(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        try:
            odds = float(row.get("offered_odds") or 0.0)
        except Exception:
            continue
        if odds <= 1.0:
            continue
        key = _selection_key(row)
        old = grouped.get(key)
        try:
            old_odds = float((old or {}).get("offered_odds") or 0.0)
        except Exception:
            old_odds = 0.0
        if old is None or odds > old_odds:
            grouped[key] = dict(row)
    return list(grouped.values())


def _pred_table_rows(db, table: str, boundary: str, *, h2h: bool, extra_where: str = "", extra_params: Sequence[Any] = ()):
    market_cols = "'h2h' AS market_key,NULL AS point" if h2h else "market_key,point"
    sql = f"""
        SELECT event_id,{market_cols},selection,offered_odds,
               bookmaker_key,bookmaker_title,status,result,created_at,settled_at,clv_pct
        FROM {table}
        WHERE created_at>=? AND offered_odds>1.0 {extra_where}
        ORDER BY created_at
    """
    rows = _query(db, sql, (boundary, *tuple(extra_params)))
    if rows:
        return rows
    sql = f"""
        SELECT event_id,{market_cols},selection,offered_odds,
               status,result,created_at
        FROM {table}
        WHERE created_at>=? AND offered_odds>1.0 {extra_where}
        ORDER BY created_at
    """
    return _query(db, sql, (boundary, *tuple(extra_params)))


def _lane_rows(db, cohort_key: str, boundary: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if cohort_key == "PRED4_ALL":
        rows.extend(_pred_table_rows(db, "football_predictive4_bets", boundary, h2h=True))
        rows.extend(_pred_table_rows(db, "football_predictive4_market_bets", boundary, h2h=False))
        return _dedupe_best_price(rows)

    if cohort_key == "PRED12_BTTS":
        for table in ("football_predictive_market_bets", "football_predictive2_market_bets"):
            rows.extend(
                _pred_table_rows(
                    db,
                    table,
                    boundary,
                    h2h=False,
                    extra_where="AND market_key='btts'",
                )
            )
        return _dedupe_best_price(rows)

    specs = {
        "NCAAF_3_TO_4_99": ("ncaaf", 3.0, 5.0),
        "NFL_2_TO_2_99": ("nfl", 2.0, 3.0),
        "EUROLEAGUE_1_50_1_99": ("basketball_euroleague", 1.5, 2.0),
        "EUROLEAGUE_3_TO_3_99": ("basketball_euroleague", 3.0, 4.0),
    }
    spec = specs.get(cohort_key)
    if not spec:
        return []
    sport, lo, hi = spec
    base = _query(
        db,
        """SELECT event_id,'h2h' AS market_key,selection,NULL AS point,offered_odds,
                  bookmaker_key,bookmaker_title,status,result,created_at,settled_at,clv_pct,sport_key
           FROM multisport_execution_bets
           WHERE created_at>=? AND offered_odds>=? AND offered_odds<?
           ORDER BY created_at""",
        (boundary, lo, hi),
    )
    if not base:
        base = _query(
            db,
            """SELECT event_id,'h2h' AS market_key,selection,NULL AS point,offered_odds,
                      status,result,created_at,sport_key
               FROM multisport_execution_bets
               WHERE created_at>=? AND offered_odds>=? AND offered_odds<?
               ORDER BY created_at""",
            (boundary, lo, hi),
        )
    for row in base:
        sk = str(row.get("sport_key") or "").lower()
        keep = sport in sk if sport in {"ncaaf", "nfl"} else sk == sport
        if keep:
            rows.append(row)
    return _dedupe_best_price(rows)


def _event_meta(db, event_id: str) -> dict[str, Any]:
    row = None
    try:
        row = db.fetchone(
            "SELECT event_id,sport_key,league,commence_time,home_team,away_team FROM events WHERE event_id=?",
            (event_id,),
        )
    except Exception:
        row = None
    if row:
        return dict(row)
    try:
        row = db.fetchone(
            """SELECT event_id,sport_key,league_title AS league,commence_time,home_team,away_team
               FROM multisport_events WHERE event_id=?""",
            (event_id,),
        )
    except Exception:
        row = None
    return dict(row or {})


def individual_bets(db) -> Dict[str, Any]:
    state = dict(portfolio.scoreboard(db))
    start = str(state.get("portfolio_started_at") or "")
    registry = {
        str(r.get("cohort_key")): dict(r)
        for r in _query(db, "SELECT * FROM outcome_edge_watch_cohorts")
    }
    by_key = {str(r["cohort_key"]): dict(r) for r in portfolio.POLICY}

    candidates = []
    for rule in portfolio.POLICY:
        if not bool(rule.get("included")) or float(rule.get("stake_gbp") or 0.0) <= 0:
            continue
        lane = str(rule.get("cohort_key") or "")
        frozen_at = str((registry.get(lane) or {}).get("frozen_at") or start)
        boundary = max(start, frozen_at)
        for row in _lane_rows(db, lane, boundary):
            created = str(row.get("created_at") or "")
            if not created or created < boundary:
                continue
            candidates.append((created, int(rule.get("priority") or 999), lane, dict(row)))

    candidates.sort(key=lambda x: (x[0], x[1], _selection_key(x[3])))
    chosen: dict[tuple[str, str, str, str], tuple[str, dict[str, Any]]] = {}
    for _, _, lane, row in candidates:
        key = _selection_key(row)
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = (lane, row)
            continue
        old_lane, old = existing
        if str(old.get("status") or "").upper() != "SETTLED" and str(row.get("status") or "").upper() == "SETTLED":
            chosen[key] = (old_lane, {**old, "status": row.get("status"), "result": row.get("result"), "settled_at": row.get("settled_at")})

    out = []
    for lane, row in chosen.values():
        rule = by_key[lane]
        meta = _event_meta(db, str(row.get("event_id") or ""))
        item = {**row}
        item.update(meta)
        item["lane"] = lane
        item["lane_label"] = str(rule.get("label") or lane)
        item["tier"] = str(rule.get("tier") or "")
        item["stake_gbp"] = float(rule.get("stake_gbp") or 0.0)
        try:
            odds = float(item.get("offered_odds") or 0.0)
        except Exception:
            odds = 0.0
        result = str(item.get("result") or "").upper().strip()
        if result in {"WIN", "WON"} and odds > 1:
            item["nominal_pnl_gbp"] = item["stake_gbp"] * (odds - 1.0)
        elif result in {"LOSS", "LOST"}:
            item["nominal_pnl_gbp"] = -item["stake_gbp"]
        elif result in {"VOID", "PUSH"}:
            item["nominal_pnl_gbp"] = 0.0
        else:
            item["nominal_pnl_gbp"] = None
        out.append(item)

    def sort_key(item: Mapping[str, Any]):
        status = str(item.get("status") or "OPEN").upper()
        open_rank = 0 if status == "OPEN" else 1
        kickoff = str(item.get("commence_time") or "9999")
        created = str(item.get("created_at") or "")
        return (open_rank, kickoff if open_rank == 0 else "", "" if open_rank == 0 else created)

    out.sort(key=sort_key)
    open_count = sum(1 for r in out if str(r.get("status") or "OPEN").upper() == "OPEN")
    settled_count = sum(1 for r in out if str(r.get("status") or "").upper() == "SETTLED")
    return {
        "version": VERSION,
        "portfolio_started_at": start,
        "bets": out,
        "total": len(out),
        "open": open_count,
        "settled": settled_count,
        "betting_authority": False,
    }


def _fmt_dt(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LONDON).strftime("%d %b %H:%M")
    except Exception:
        return raw[:16]


def _money(value: Any) -> str:
    if value is None:
        return "—"
    try:
        x = float(value)
        return f"{'+' if x > 0 else ''}£{x:.2f}"
    except Exception:
        return "—"


def _num(value: Any, digits: int = 2, suffix: str = "") -> str:
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except Exception:
        return "—"


def _market(row: Mapping[str, Any]) -> str:
    market = str(row.get("market_key") or "").upper()
    point = row.get("point")
    if point is None or point == "":
        return market or "—"
    return f"{market} {_num(point,1)}"


def _status(row: Mapping[str, Any]) -> str:
    status = str(row.get("status") or "OPEN").upper()
    result = str(row.get("result") or "").upper()
    if status == "SETTLED" and result:
        return result
    return status


def _details_html(db) -> str:
    try:
        report = individual_bets(db)
    except Exception as exc:
        return (
            "<details class='panel' style='margin-top:12px'><summary><strong>Individual frozen shadow bets</strong></summary>"
            f"<div class='muted' style='margin-top:10px'>Unable to render details: {escape(type(exc).__name__)}</div></details>"
        )

    rows = []
    for bet in report.get("bets") or []:
        event = f"{bet.get('home_team') or '?'} v {bet.get('away_team') or '?'}"
        book = str(bet.get("bookmaker_title") or bet.get("bookmaker_key") or "—")
        status = _status(bet)
        tone = "good" if status in {"WIN", "WON"} else "badx" if status in {"LOSS", "LOST"} else ""
        rows.append(
            "<tr>"
            f"<td>{escape(_fmt_dt(bet.get('commence_time')))}</td>"
            f"<td><strong>{escape(event)}</strong><div class='muted'>{escape(str(bet.get('league') or bet.get('sport_key') or ''))}</div></td>"
            f"<td>{escape(str(bet.get('lane_label') or bet.get('lane') or ''))}<div class='muted'>{escape(str(bet.get('tier') or ''))}</div></td>"
            f"<td>{escape(_market(bet))}</td>"
            f"<td>{escape(str(bet.get('selection') or '—'))}</td>"
            f"<td>{_num(bet.get('offered_odds'),2)}</td>"
            f"<td>{escape(book)}</td>"
            f"<td>£{float(bet.get('stake_gbp') or 0.0):.2f}</td>"
            f"<td class='{tone}'><strong>{escape(status)}</strong><div class='muted'>{escape(_fmt_dt(bet.get('settled_at'))) if bet.get('settled_at') else ''}</div></td>"
            f"<td class='{tone}'>{_money(bet.get('nominal_pnl_gbp'))}</td>"
            f"<td>{_num(bet.get('clv_pct'),1,'%')}</td>"
            "</tr>"
        )

    body = "".join(rows) or "<tr><td colspan='11' class='muted'>No frozen forward bets recorded yet.</td></tr>"
    return f"""
    <details class='panel' style='margin-top:12px'>
      <summary style='cursor:pointer'><strong>Individual frozen shadow bets</strong> · {int(report.get('open') or 0)} open · {int(report.get('settled') or 0)} settled</summary>
      <div class='muted compact-note' style='margin:10px 0'>Exact frozen entry odds and reference stake are shown. Duplicate underlying selections are displayed once using the same lane-priority rules as the headline portfolio. Times are Europe/London.</div>
      <div class='table-wrap'><table>
        <thead><tr><th>Kickoff</th><th>Event</th><th>Lane</th><th>Market</th><th>Selection</th><th>Odds</th><th>Book</th><th>Stake</th><th>Settlement</th><th>Nominal P&amp;L</th><th>CLV</th></tr></thead>
        <tbody>{body}</tbody>
      </table></div>
    </details>
    """


def install(app, db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SECTION
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_SECTION = dashboard._section

    def section_with_individual_bets(db_obj):
        html = _ORIGINAL_SECTION(db_obj)
        detail = _details_html(db_obj)
        if "</section>" in html:
            head, tail = html.rsplit("</section>", 1)
            return head + detail + "</section>" + tail
        return html + detail

    dashboard._section = section_with_individual_bets
    dashboard.DASHBOARD_VERSION = DASHBOARD_VERSION
    dashboard_layout_v2.VERSION = DASHBOARD_VERSION

    @app.get("/api/frozen-shadow-bets")
    def frozen_shadow_bets_api():
        return individual_bets(db)

    report = individual_bets(db)
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "dashboard_version": DASHBOARD_VERSION,
        "total": int(report.get("total") or 0),
        "open": int(report.get("open") or 0),
        "settled": int(report.get("settled") or 0),
        "presentation_only": True,
        "betting_authority": False,
    }
