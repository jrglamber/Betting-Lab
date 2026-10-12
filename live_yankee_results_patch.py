from __future__ import annotations

"""Settlement/accounting and detailed dashboard for the Live Yankee Feed.

Research/manual only.  Each live-feed leg is linked back to its original shadow
selection and inherits that settled result.  System returns are then calculated
from the frozen live-feed entry odds using the actual Double/Trixie/Yankee line
structure and the feed's £10 total reference stake.
"""

from html import escape
from itertools import combinations
import json
from typing import Any, Dict, Mapping, Sequence

import live_yankee_feed as feed
import web as web_module
from db import utc_now_iso

VERSION = "YANKEE_LIVE_FEED_V4_CARD_RESULTS"
REFERENCE_STAKE_GBP = 10.0
SOURCE_TABLES = {
    "execution_shadow_bets",
    "multisport_execution_bets",
    "football_predictive4_bets",
    "football_predictive4_market_bets",
    "api_sports_pred_shadow_bets",
}

_INSTALLED = False
_ORIGINAL_SEND = None
_ORIGINAL_PAGE = None
_ORIGINAL_SCOREBOARD = None


def _ensure_schema(db) -> None:
    feed._ensure_schema(db)
    card_columns = (
        "ALTER TABLE live_yankee_cards ADD COLUMN result TEXT",
        "ALTER TABLE live_yankee_cards ADD COLUMN winning_legs INTEGER",
        "ALTER TABLE live_yankee_cards ADD COLUMN return_units REAL",
        "ALTER TABLE live_yankee_cards ADD COLUMN pnl_units REAL",
        "ALTER TABLE live_yankee_cards ADD COLUMN roi_pct REAL",
        "ALTER TABLE live_yankee_cards ADD COLUMN reference_stake_gbp REAL",
        "ALTER TABLE live_yankee_cards ADD COLUMN reference_return_gbp REAL",
        "ALTER TABLE live_yankee_cards ADD COLUMN reference_pnl_gbp REAL",
        "ALTER TABLE live_yankee_cards ADD COLUMN settled_at TEXT",
    )
    leg_columns = (
        "ALTER TABLE live_yankee_legs ADD COLUMN result TEXT",
        "ALTER TABLE live_yankee_legs ADD COLUMN settled_at TEXT",
    )
    for sql in card_columns + leg_columns:
        try:
            db.execute(sql)
        except Exception:
            pass
    try:
        db.execute(
            "UPDATE live_yankee_cards SET reference_stake_gbp=? "
            "WHERE reference_stake_gbp IS NULL",
            (REFERENCE_STAKE_GBP,),
        )
    except Exception:
        pass


def _normalize_result(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if text in {"WIN", "WON", "WINNER"}:
        return "WIN"
    if text in {"LOSS", "LOSE", "LOST", "LOSER"}:
        return "LOSS"
    if text in {"PUSH", "VOID", "REFUND", "CANCELLED", "CANCELED"}:
        return "VOID" if text != "PUSH" else "PUSH"
    return None


def _source_result(db, leg: Mapping[str, Any]) -> str | None:
    candidate = db.fetchone(
        "SELECT source_table,source_id FROM live_yankee_candidates WHERE id=?",
        (int(leg["candidate_id"]),),
    ) or {}
    table = str(candidate.get("source_table") or "")
    source_id = candidate.get("source_id")
    if table not in SOURCE_TABLES or source_id is None:
        return None
    try:
        row = db.fetchone(f"SELECT * FROM {table} WHERE id=?", (int(source_id),)) or {}
    except Exception:
        return None
    result = _normalize_result(row.get("result"))
    if result:
        return result
    status = str(row.get("status") or "").upper()
    if status in {"VOID", "CANCELLED", "CANCELED"}:
        return "VOID"
    # Some shadow lanes expose P&L before/without a textual result.  Positive and
    # negative one-unit settlement is unambiguous; zero remains unresolved unless
    # the lane explicitly labels it PUSH/VOID.
    if status == "SETTLED" and row.get("pnl_units") is not None:
        try:
            pnl = float(row.get("pnl_units"))
            if pnl > 1e-12:
                return "WIN"
            if pnl < -1e-12:
                return "LOSS"
        except Exception:
            pass
    return None


def _system_type(card: Mapping[str, Any], leg_count: int) -> str:
    explicit = str(card.get("system_type") or "").upper()
    if explicit in {"DOUBLE", "TRIXIE", "YANKEE"}:
        return explicit
    return "YANKEE" if leg_count == 4 else "TRIXIE" if leg_count == 3 else "DOUBLE"


def _line_combos(system_type: str, leg_orders: Sequence[int]) -> list[tuple[int, ...]]:
    orders = tuple(int(x) for x in leg_orders)
    if system_type == "YANKEE" and len(orders) == 4:
        sizes = (2, 3, 4)
    elif system_type == "TRIXIE" and len(orders) == 3:
        sizes = (2, 3)
    elif system_type == "DOUBLE" and len(orders) == 2:
        sizes = (2,)
    else:
        return []
    out: list[tuple[int, ...]] = []
    for size in sizes:
        out.extend(combinations(orders, size))
    return out


def _settle_one(db, card: Mapping[str, Any]) -> bool:
    cid = int(card["id"])
    legs = [dict(x) for x in db.fetchall(
        "SELECT * FROM live_yankee_legs WHERE card_id=? ORDER BY leg_order",
        (cid,),
    )]
    if len(legs) not in {2, 3, 4}:
        return False

    changed = False
    for leg in legs:
        result = _normalize_result(leg.get("result")) or _source_result(db, leg)
        if result and result != _normalize_result(leg.get("result")):
            db.execute(
                "UPDATE live_yankee_legs SET result=?,settled_at=? WHERE id=?",
                (result, utc_now_iso(), int(leg["id"])),
            )
            leg["result"] = result
            changed = True

    if any(_normalize_result(x.get("result")) is None for x in legs):
        return changed

    system_type = _system_type(card, len(legs))
    combos = _line_combos(system_type, [int(x["leg_order"]) for x in legs])
    if not combos:
        return changed
    by_order = {int(x["leg_order"]): x for x in legs}
    line_stake = 1.0 / float(len(combos))
    total_return = 0.0
    for combo in combos:
        settled_odds = 1.0
        lost = False
        for order in combo:
            leg = by_order[order]
            result = _normalize_result(leg.get("result"))
            if result == "LOSS":
                lost = True
                settled_odds = 0.0
                break
            if result == "WIN":
                settled_odds *= float(leg.get("entry_odds") or 0.0)
            elif result in {"PUSH", "VOID"}:
                settled_odds *= 1.0
        if not lost:
            total_return += line_stake * settled_odds

    pnl_units = total_return - 1.0
    roi_pct = pnl_units * 100.0
    winning_legs = sum(1 for x in legs if _normalize_result(x.get("result")) == "WIN")
    result = "PROFIT" if pnl_units > 1e-12 else "LOSS" if pnl_units < -1e-12 else "BREAKEVEN"
    return_gbp = total_return * REFERENCE_STAKE_GBP
    pnl_gbp = pnl_units * REFERENCE_STAKE_GBP
    db.execute(
        """UPDATE live_yankee_cards
           SET status='SETTLED',result=?,winning_legs=?,return_units=?,pnl_units=?,roi_pct=?,
               reference_stake_gbp=?,reference_return_gbp=?,reference_pnl_gbp=?,settled_at=?
           WHERE id=?""",
        (
            result, winning_legs, total_return, pnl_units, roi_pct,
            REFERENCE_STAKE_GBP, return_gbp, pnl_gbp, utc_now_iso(), cid,
        ),
    )
    return True


def settle_cards(db) -> Dict[str, int]:
    _ensure_schema(db)
    cards = db.fetchall(
        "SELECT * FROM live_yankee_cards WHERE status!='SETTLED' OR result IS NULL ORDER BY id"
    )
    touched = 0
    settled = 0
    for card in cards:
        before = str(card.get("status") or "")
        if _settle_one(db, card):
            touched += 1
        refreshed = db.fetchone("SELECT status FROM live_yankee_cards WHERE id=?", (int(card["id"]),)) or {}
        if before != "SETTLED" and str(refreshed.get("status") or "") == "SETTLED":
            settled += 1
    return {"touched": touched, "settled": settled}


def _send(db, settings, limit: int = 5):
    # The fast one-minute loop calls feed._send dynamically, so settlement rides
    # the existing low-latency loop without adding another thread or API call.
    settle_cards(db)
    return _ORIGINAL_SEND(db, settings, limit=limit)


def _tone_result(value: Any) -> str:
    text = str(value or "").upper()
    if text in {"WIN", "PROFIT"}:
        return "ok"
    if text in {"LOSS"}:
        return "bad"
    if text in {"PUSH", "VOID", "BREAKEVEN"}:
        return "warn"
    return ""


def _money(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"£{float(value):+.2f}" if abs(float(value)) > 1e-12 else "£0.00"
    except Exception:
        return "—"


def _market_label(leg: Mapping[str, Any]) -> str:
    return feed._market_label(leg)


def _card_detail_html(db, card: Mapping[str, Any]) -> str:
    legs = [dict(x) for x in db.fetchall(
        "SELECT * FROM live_yankee_legs WHERE card_id=? ORDER BY leg_order",
        (int(card["id"]),),
    )]
    system_type = _system_type(card, len(legs))
    status = str(card.get("status") or "OPEN")
    result = str(card.get("result") or "PENDING")
    pnl = card.get("reference_pnl_gbp")
    pnl_css = "ok" if pnl is not None and float(pnl) > 1e-12 else "bad" if pnl is not None and float(pnl) < -1e-12 else "warn" if pnl is not None else ""
    header_result = _money(pnl) if pnl is not None else "PENDING"
    rows = []
    for leg in legs:
        leg_result = str(leg.get("result") or "PENDING").upper()
        fixture = f"{leg.get('home_team') or ''} v {leg.get('away_team') or ''}".strip()
        rows.append(
            "<tr>"
            f"<td>{int(leg.get('leg_order') or 0)}</td>"
            f"<td>{escape(str(leg.get('source_lane') or ''))}</td>"
            f"<td>{escape(fixture)}</td>"
            f"<td><strong>{escape(_market_label(leg))}</strong></td>"
            f"<td>{float(leg.get('entry_odds') or 0):.2f}</td>"
            f"<td>{escape(str(leg.get('bookmaker_title') or leg.get('bookmaker_key') or ''))}</td>"
            f"<td>{feed._fmt_time(leg.get('commence_time'))}</td>"
            f"<td class='{_tone_result(leg_result)}'><strong>{escape(leg_result)}</strong></td>"
            "</tr>"
        )
    leg_rows = "".join(rows) or "<tr><td colspan='8'>No legs recorded.</td></tr>"
    settled_summary = (
        f"Winning legs <strong>{int(card.get('winning_legs') or 0)}/{len(legs)}</strong> · "
        f"Return <strong>{_money(card.get('reference_return_gbp'))}</strong> · "
        f"P/L <strong class='{pnl_css}'>{_money(pnl)}</strong> · "
        f"ROI <strong class='{pnl_css}'>{float(card.get('roi_pct') or 0):+.1f}%</strong>"
        if status == "SETTLED" and pnl is not None
        else "Awaiting all underlying selections to settle."
    )
    synthetic_note = (
        "<div class='muted' style='margin-top:8px'>Cross-book synthetic P/L is a research calculation from the recorded leg prices; it was not one verified single-book system bet.</div>"
        if str(card.get("assembly_mode") or "") == "CROSS_BOOK_SYNTHETIC" else ""
    )
    return (
        "<div class='card' style='margin:14px 0'>"
        f"<div style='display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap'>"
        f"<div><strong>{escape(system_type.title())} #{int(card['id'])}</strong> · {escape(str(card.get('assembly_mode') or ''))}</div>"
        f"<div class='{pnl_css}'><strong>{escape(result)} · {escape(header_result)}</strong></div></div>"
        f"<div class='muted' style='margin-top:6px'>Created {feed._fmt_time(card.get('created_at'))} · Sources {escape(str(card.get('source_mix') or '—'))} · Books {escape(str(card.get('bookmaker_mix') or '—'))}</div>"
        "<div class='table-wrap' style='margin-top:10px'><table><thead><tr>"
        "<th>#</th><th>Source</th><th>Event</th><th>Pick</th><th>Odds</th><th>Book</th><th>Kickoff</th><th>Result</th>"
        f"</tr></thead><tbody>{leg_rows}</tbody></table></div>"
        f"<div class='summary' style='margin-top:10px'>{settled_summary}</div>{synthetic_note}</div>"
    )


def _page_html():
    # Read-through settlement makes the dashboard correct even if the one-minute
    # companion loop has temporarily been asleep/redeploying.
    try:
        settle_cards(web_module.db)
    except Exception:
        pass
    html = _ORIGINAL_PAGE()
    try:
        cards = web_module.db.fetchall("SELECT * FROM live_yankee_cards ORDER BY id DESC LIMIT 40")
        details = "".join(_card_detail_html(web_module.db, c) for c in cards)
    except Exception as exc:
        details = f"<div class='bad'>Card detail error: {escape(type(exc).__name__)}</div>"
    if not details:
        details = "<div class='muted'>No system cards formed yet.</div>"
    panel = (
        "<div class='panel'><h2>Yankee card details & results</h2>"
        "<div class='muted'>Each card shows its frozen picks and prices. Results inherit from the original shadow selections. "
        "P/L uses a £10 total reference stake split equally across the actual system lines: 11 for a Yankee, 4 for a Trixie, 1 for a Double.</div>"
        f"{details}</div>"
    )
    marker = "<div class='panel'><h2>Expiry fallback</h2>"
    if marker in html:
        return html.replace(marker, panel + marker, 1)
    return html.replace("</body>", panel + "</body>", 1)


def _scoreboard(db):
    _ensure_schema(db)
    out = dict(_ORIGINAL_SCOREBOARD(db))
    rows = db.fetchall("SELECT * FROM live_yankee_cards WHERE status='SETTLED'")
    pnl = sum(float(x.get("reference_pnl_gbp") or 0.0) for x in rows)
    out["settled_cards"] = len(rows)
    out["reference_pnl_gbp"] = pnl
    out["reference_stake_gbp_per_card"] = REFERENCE_STAKE_GBP
    return out


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SEND, _ORIGINAL_PAGE, _ORIGINAL_SCOREBOARD
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    _ensure_schema(web_module.db)
    _ORIGINAL_SEND = feed._send
    _ORIGINAL_PAGE = feed._page_html
    _ORIGINAL_SCOREBOARD = feed.scoreboard
    feed._send = _send
    feed._page_html = _page_html
    feed.scoreboard = _scoreboard
    try:
        settle_cards(web_module.db)
    except Exception:
        pass
    feed.ALGORITHM_VERSION = VERSION
    web_module.VERSION = "0.19.49"
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "reference_stake_gbp": REFERENCE_STAKE_GBP,
        "settlement_source": "original_shadow_selection",
        "dashboard_version": web_module.VERSION,
    }
