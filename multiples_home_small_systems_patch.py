from __future__ import annotations

"""Surface canonical forward multiples-stream metrics on the main dashboard.

The curated homepage originally compressed multiples research to cards, settled,
ROI and a queue note. The Hit-Rate Lab's Forward streams table is more useful for
monitoring the experiments, so this presentation-only patch mirrors those fields
on the homepage and adds cumulative profit in units. No selection, card,
provider, staking or betting-authority rules are changed.
"""

from html import escape
import re
from typing import Any, Dict, Mapping

import dashboard_layout_v2
import hit_rate_multiples_lab as hr
import live_yankee_feed

VERSION = "MULTIPLES_HOME_FORWARD_STREAMS_V2"
_INSTALLED = False
_ORIGINAL_PAGE = None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _fmt_number(value: Any, digits: int = 2, suffix: str = "") -> str:
    try:
        if value is None:
            return "—"
        return f"{float(value):.{digits}f}{suffix}"
    except Exception:
        return "—"


def _tone(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return ""
    return "good" if number > 0 else "badx" if number < 0 else ""


def _live_queued_events(db) -> int:
    try:
        row = db.fetchone(
            "SELECT COUNT(DISTINCT event_id) AS n "
            "FROM live_yankee_candidates WHERE status='QUEUED'"
        ) or {}
        return _int(row.get("n"))
    except Exception:
        return 0


def _live_row(db) -> str:
    try:
        live = dict(live_yankee_feed.scoreboard(db) or {})
    except Exception:
        live = {}
    return (
        "<tr>"
        "<td><a href='/live-yankee'><strong>LIVE</strong></a></td>"
        "<td>Yankee Feed</td>"
        f"<td>{_int(live.get('queued'))}</td>"
        f"<td>{_live_queued_events(db)}</td>"
        f"<td>{_int(live.get('cards'))}</td>"
        "<td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td>"
        "</tr>"
    )


def _stream_row(pool: str, system: str, metrics: Mapping[str, Any]) -> str:
    pnl = metrics.get("pnl_units")
    roi = metrics.get("roi_pct")
    return (
        "<tr>"
        "<td><a href='/hit-rate-multiples'><strong>" + escape(pool) + "</strong></a></td>"
        "<td>" + escape(system.title()) + "</td>"
        f"<td>{_int(metrics.get('queued'))}</td>"
        f"<td>{_int(metrics.get('queued_events'))}</td>"
        f"<td>{_int(metrics.get('cards'))}</td>"
        f"<td>{_int(metrics.get('settled'))}</td>"
        f"<td>{_int(metrics.get('profitable'))}</td>"
        f"<td>{_int(metrics.get('at_least_3'))}</td>"
        f"<td>{_int(metrics.get('full_house'))}</td>"
        f"<td class='{_tone(pnl)}'>{_fmt_number(pnl, 2, 'u')}</td>"
        f"<td class='{_tone(roi)}'>{_fmt_number(roi, 1, '%')}</td>"
        "</tr>"
    )


def _forward_rows(db) -> str:
    try:
        streams = dict((hr.scoreboard(db) or {}).get("streams") or {})
    except Exception:
        streams = {}

    preferred_pools = ["HR1", "HR2"]
    pools = preferred_pools + sorted(k for k in streams if k not in preferred_pools)
    preferred_systems = ["YANKEE", "HEINZ", "DOUBLE", "TRIXIE"]
    rows = [_live_row(db)]

    for pool in pools:
        systems = dict(streams.get(pool) or {})
        order = preferred_systems + sorted(k for k in systems if k not in preferred_systems)
        for system in order:
            if system not in systems:
                continue
            rows.append(_stream_row(pool, system, dict(systems.get(system) or {})))
    return "".join(rows)


def _replace_multiples_table(html: str, db) -> str:
    section_start = html.find("<section id='multiples'>")
    if section_start < 0:
        return html
    section_end = html.find("</section>", section_start)
    if section_end < 0:
        return html

    section = html[section_start:section_end]
    table = (
        "<table><thead><tr>"
        "<th>Pool</th><th>System</th><th>Queue</th><th>Events</th>"
        "<th>Cards</th><th>Settled</th><th>Profitable</th><th>3+ wins</th>"
        "<th>All legs</th><th>Profit (u)</th><th>ROI</th>"
        "</tr></thead><tbody>" + _forward_rows(db) + "</tbody></table>"
    )
    replaced, count = re.subn(
        r"<table><thead><tr>.*?</tr></thead><tbody>.*?</tbody></table>",
        table,
        section,
        count=1,
        flags=re.DOTALL,
    )
    if count != 1:
        return html

    note = (
        "<div class='muted compact-note' style='margin-top:10px'>"
        "Mirrors the Hit-Rate Lab forward-stream view. Profit (u) is cumulative "
        "settled-card P&amp;L; live Yankee settlement metrics are not tracked in that feed."
        "</div>"
    )
    nav_marker = "<div class='nav' style='margin-top:12px'>"
    if note not in replaced and nav_marker in replaced:
        replaced = replaced.replace(nav_marker, note + nav_marker, 1)

    return html[:section_start] + replaced + html[section_end:]


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_PAGE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_PAGE = dashboard_layout_v2._page

    def _page_with_forward_streams(db, base_style: str) -> str:
        html = _ORIGINAL_PAGE(db, base_style)
        return _replace_multiples_table(html, db)

    dashboard_layout_v2._page = _page_with_forward_streams
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "columns": [
            "Pool", "System", "Queue", "Events", "Cards", "Settled",
            "Profitable", "3+ wins", "All legs", "Profit (u)", "ROI",
        ],
        "source": "canonical hit_rate_multiples_lab.scoreboard streams",
        "live_yankee_preserved": True,
        "presentation_only": True,
        "selection_rules_changed": False,
        "provider_calls_added": 0,
        "betting_authority_changed": False,
    }
