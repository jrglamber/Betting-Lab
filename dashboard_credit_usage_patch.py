from __future__ import annotations

"""Add provider-credit observability near the top of the curated dashboard.

Presentation only.  Usage is derived from the existing collector_runs
actual_cost ledger, matching the legacy dashboard's accounting source.  The
seven-day average uses seven completed UTC days (today is shown separately),
then projects that run-rate over 30 days against the 100k provider allowance.
"""

from datetime import datetime, time, timedelta, timezone
from html import escape
from typing import Any, Dict

import dashboard_layout_v2

VERSION = "DASHBOARD_PROVIDER_CREDITS_V1"
MONTHLY_PROVIDER_BUDGET = 100_000
_INSTALLED = False
_ORIGINAL_PAGE = None


def _usage(db) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    today = now.date()
    start_day = today - timedelta(days=7)
    start_at = datetime.combine(start_day, time.min, tzinfo=timezone.utc).isoformat()

    try:
        rows = db.fetchall(
            """
            SELECT SUBSTR(started_at,1,10) AS day,
                   COALESCE(SUM(actual_cost),0) AS credits
            FROM collector_runs
            WHERE started_at >= ?
            GROUP BY SUBSTR(started_at,1,10)
            ORDER BY day
            """,
            (start_at,),
        )
    except Exception:
        rows = []

    by_day = {
        str(row.get("day")): int(row.get("credits") or 0)
        for row in rows
        if row.get("day")
    }
    today_used = by_day.get(today.isoformat(), 0)
    yesterday_used = by_day.get((today - timedelta(days=1)).isoformat(), 0)
    completed = [
        by_day.get((today - timedelta(days=i)).isoformat(), 0)
        for i in range(1, 8)
    ]
    avg7 = sum(completed) / 7.0
    projected30 = avg7 * 30.0
    projected_pct = (
        projected30 / MONTHLY_PROVIDER_BUDGET * 100.0
        if MONTHLY_PROVIDER_BUDGET else 0.0
    )
    return {
        "today": today_used,
        "yesterday": yesterday_used,
        "avg7": avg7,
        "projected30": projected30,
        "budget": MONTHLY_PROVIDER_BUDGET,
        "projected_pct": projected_pct,
    }


def _metric(label: str, value: str, note: str = "", css: str = "") -> str:
    return (
        "<div class='card metric-card'>"
        f"<div class='label'>{escape(label)}</div>"
        f"<div class='value {escape(css)}'>{escape(value)}</div>"
        f"<div class='muted'>{escape(note)}</div></div>"
    )


def _panel(db) -> str:
    u = _usage(db)
    projection_css = "good" if float(u["projected30"]) <= float(u["budget"]) else "badx"
    budget_note = f"30d projection = {float(u['projected_pct']):.1f}% of allowance"
    cards = "".join(
        (
            _metric("Credits today", f"{int(u['today']):,}", "provider actual-cost ledger · UTC"),
            _metric("Yesterday", f"{int(u['yesterday']):,}", "completed UTC day"),
            _metric("7-day avg/day", f"{float(u['avg7']):,.0f}", "seven completed days"),
            _metric("30-day projection", f"{float(u['projected30']):,.0f}", budget_note, projection_css),
            _metric("Total provider budget", f"{int(u['budget']):,}", "monthly credit allowance"),
        )
    )
    return (
        "<section id='credits'>"
        "<div class='section-title'><div><h2>Provider Credits</h2>"
        "<div class='muted'>Credit limits inside Betting Lab are removed; this keeps the real provider allowance visible while we use the quota fully.</div>"
        "</div></div>"
        f"<div class='grid compact'>{cards}</div>"
        "</section>"
    )


def install(db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_PAGE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_PAGE = dashboard_layout_v2._page

    def page_with_provider_credits(db_obj, base_style: str) -> str:
        html = _ORIGINAL_PAGE(db_obj, base_style)
        if "id='credits'" not in html:
            marker = "<section id='leaderboard'>"
            panel = _panel(db_obj)
            if marker in html:
                html = html.replace(marker, panel + "\n\n    " + marker, 1)
            else:
                html = html.replace("</div>\n\n    <section id='overview'>", "</div>\n" + panel + "\n<section id='overview'>", 1)
        if "href='#credits'" not in html:
            nav_marker = "<a href='#overview'>Overview</a>"
            html = html.replace(nav_marker, nav_marker + "<a href='#credits'>Credits</a>", 1)
        return html

    dashboard_layout_v2._page = page_with_provider_credits
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "monthly_provider_budget": MONTHLY_PROVIDER_BUDGET,
        "placement": "after_executive_snapshot",
    }
