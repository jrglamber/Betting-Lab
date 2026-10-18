from __future__ import annotations

"""Add provider-credit observability and spend attribution to the dashboard.

Presentation only. Usage is derived from the existing collector_runs actual_cost
ledger. The seven-day average uses seven completed UTC days (today is shown
separately), then projects that run-rate over 30 days against the provider
allowance. A run-type/sport breakdown makes marginal paid spend auditable before
we disable or throttle research lanes.
"""

from datetime import datetime, time, timedelta, timezone
from html import escape
from typing import Any, Dict, List

import dashboard_layout_v2

VERSION = "DASHBOARD_PROVIDER_CREDITS_V2_1"
MONTHLY_PROVIDER_BUDGET = 100_000
_INSTALLED = False
_ORIGINAL_PAGE = None


def _period_breakdown(db, start_at: str, end_at: str) -> List[Dict[str, Any]]:
    try:
        return db.fetchall(
            """
            SELECT run_type,
                   COALESCE(sport_key,'') AS sport_key,
                   COALESCE(SUM(actual_cost),0) AS credits,
                   COUNT(*) AS runs
            FROM collector_runs
            WHERE started_at >= ? AND started_at < ? AND actual_cost > 0
            GROUP BY run_type, COALESCE(sport_key,'')
            ORDER BY credits DESC, run_type, sport_key
            """,
            (start_at, end_at),
        )
    except Exception:
        return []


def _usage(db) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    today = now.date()
    start_day = today - timedelta(days=7)
    start_at = datetime.combine(start_day, time.min, tzinfo=timezone.utc).isoformat()
    today_start = datetime.combine(today, time.min, tzinfo=timezone.utc)
    yesterday_start = today_start - timedelta(days=1)

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

    seven = _period_breakdown(
        db,
        datetime.combine(today - timedelta(days=7), time.min, tzinfo=timezone.utc).isoformat(),
        today_start.isoformat(),
    )
    yesterday = _period_breakdown(db, yesterday_start.isoformat(), today_start.isoformat())
    ymap = {
        (str(r.get("run_type") or ""), str(r.get("sport_key") or "")): int(r.get("credits") or 0)
        for r in yesterday
    }
    top_spend = []
    for row in seven[:20]:
        run_type = str(row.get("run_type") or "UNKNOWN")
        sport_key = str(row.get("sport_key") or "")
        seven_credits = int(row.get("credits") or 0)
        top_spend.append(
            {
                "run_type": run_type,
                "sport_key": sport_key,
                "yesterday": ymap.get((run_type, sport_key), 0),
                "seven_days": seven_credits,
                "avg_day": seven_credits / 7.0,
                "runs": int(row.get("runs") or 0),
            }
        )

    # Aggregate the same ledger by run family as well. This is the cleanest
    # answer to 'which subsystem should we throttle?' because a single subsystem
    # may fan out across many sports.
    seven_by_type: Dict[str, int] = {}
    seven_runs_by_type: Dict[str, int] = {}
    yesterday_by_type: Dict[str, int] = {}
    for row in seven:
        key = str(row.get("run_type") or "UNKNOWN")
        seven_by_type[key] = seven_by_type.get(key, 0) + int(row.get("credits") or 0)
        seven_runs_by_type[key] = seven_runs_by_type.get(key, 0) + int(row.get("runs") or 0)
    for row in yesterday:
        key = str(row.get("run_type") or "UNKNOWN")
        yesterday_by_type[key] = yesterday_by_type.get(key, 0) + int(row.get("credits") or 0)
    run_type_totals = [
        {
            "run_type": key,
            "yesterday": yesterday_by_type.get(key, 0),
            "seven_days": credits,
            "avg_day": credits / 7.0,
            "runs": seven_runs_by_type.get(key, 0),
        }
        for key, credits in seven_by_type.items()
    ]
    run_type_totals.sort(key=lambda row: int(row["seven_days"]), reverse=True)

    return {
        "today": today_used,
        "yesterday": yesterday_used,
        "avg7": avg7,
        "projected30": projected30,
        "budget": MONTHLY_PROVIDER_BUDGET,
        "projected_pct": projected_pct,
        "top_spend": top_spend,
        "run_type_totals": run_type_totals,
    }


def _metric(label: str, value: str, note: str = "", css: str = "") -> str:
    return (
        "<div class='card metric-card'>"
        f"<div class='label'>{escape(label)}</div>"
        f"<div class='value {escape(css)}'>{escape(value)}</div>"
        f"<div class='muted'>{escape(note)}</div></div>"
    )


def _spend_table(rows: List[Dict[str, Any]], label_key: str = "sport_key") -> str:
    if not rows:
        return "<div class='muted'>No paid-run attribution available yet.</div>"
    body = []
    for row in rows[:12]:
        lane = str(row["run_type"])
        secondary = str(row.get(label_key) or "") if label_key else ""
        subtitle = f"<div class='muted'>{escape(secondary)}</div>" if secondary else ""
        body.append(
            "<tr>"
            f"<td><strong>{escape(lane)}</strong>{subtitle}</td>"
            f"<td>{int(row['yesterday']):,}</td>"
            f"<td>{int(row['seven_days']):,}</td>"
            f"<td>{float(row['avg_day']):,.0f}</td>"
            f"<td>{int(row['runs']):,}</td>"
            "</tr>"
        )
    return (
        "<div class='table-wrap'><table><thead><tr>"
        "<th>Paid run / sport</th><th>Yesterday</th><th>7d credits</th>"
        "<th>Avg/day</th><th>Runs</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
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
        "<div class='muted'>Actual paid-provider spend. Attribution shows which paid subsystem and sport consume the allowance so weak lanes can be cut selectively.</div>"
        "</div></div>"
        f"<div class='grid compact'>{cards}</div>"
        "<h3>Spend by subsystem</h3>"
        f"{_spend_table(list(u['run_type_totals']), label_key='')}"
        "<h3>Top paid sport lanes</h3>"
        f"{_spend_table(list(u['top_spend']))}"
        "</section>"
    )


def install(db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_PAGE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    diagnostic = _usage(db)
    print(
        "PROVIDER_CREDIT_BREAKDOWN "
        + str(
            {
                "yesterday": diagnostic["yesterday"],
                "avg7": round(float(diagnostic["avg7"]), 2),
                "projected30": round(float(diagnostic["projected30"]), 2),
                "run_type_totals": list(diagnostic["run_type_totals"])[:12],
                "top_spend": list(diagnostic["top_spend"])[:12],
            }
        ),
        flush=True,
    )

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
        "spend_attribution": True,
    }
