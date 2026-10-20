from __future__ import annotations

"""Expose the frozen singles shadow portfolio on the curated homepage.

Presentation only. The portfolio accounting and freeze rules live in
frozen_shadow_portfolio_patch; this module only renders their current state.
"""

from datetime import datetime
from html import escape
from typing import Any, Dict

import dashboard_layout_v2
import frozen_shadow_portfolio_patch as portfolio

VERSION = "FROZEN_SHADOW_DASHBOARD_V1"
DASHBOARD_VERSION = "0.19.75"
_INSTALLED = False
_ORIGINAL_PAGE = None


def _fmt(value: Any, digits: int = 1, suffix: str = "") -> str:
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except Exception:
        return "—"


def _money(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return "—"
    sign = "+" if x > 0 else ""
    return f"{sign}£{x:,.2f}"


def _start(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y %H:%M UTC")
    except Exception:
        return raw


def _tone(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return ""
    return "good" if x > 0 else "badx" if x < 0 else ""


def _section(db) -> str:
    try:
        state = portfolio.scoreboard(db)
    except Exception as exc:
        return (
            "<section id='shadow'><div class='section-title'><div><h2>Frozen Shadow Portfolio</h2>"
            "<div class='muted'>Production-style forward test</div></div></div>"
            f"<div class='panel'><span class='pill warn'>CHECK</span> "
            f"Unable to render portfolio state: {escape(type(exc).__name__)}</div></section>"
        )

    settled = int(state.get("settled_unique_bets") or 0)
    pnl = state.get("pnl_gbp")
    roi = state.get("roi_pct")
    turnover = state.get("turnover_gbp")
    rules = "FROZEN" if state.get("rules_frozen") else "CHECK"
    authority = "SHADOW ONLY" if not state.get("betting_authority") else "LIVE"

    metrics = "".join([
        f"<div class='card metric-card'><div class='label'>Unique settled</div><div class='value'>{settled}</div><div class='muted'>deduplicated portfolio bets</div></div>",
        f"<div class='card metric-card'><div class='label'>Nominal turnover</div><div class='value'>{_money(turnover)}</div><div class='muted'>frozen reference stakes</div></div>",
        f"<div class='card metric-card'><div class='label'>Nominal P&amp;L</div><div class='value {_tone(pnl)}'>{_money(pnl)}</div><div class='muted'>no real money placed</div></div>",
        f"<div class='card metric-card'><div class='label'>Portfolio ROI</div><div class='value {_tone(roi)}'>{_fmt(roi,1,'%')}</div><div class='muted'>first review at 50 bets</div></div>",
    ])

    rows = []
    for lane in state.get("lanes") or []:
        tier = str(lane.get("tier") or "")
        checkpoint = str(lane.get("checkpoint") or "ACCUMULATING")
        stake = float(lane.get("stake_gbp") or 0.0)
        rows.append(
            "<tr>"
            f"<td><strong>{escape(str(lane.get('label') or lane.get('cohort_key') or ''))}</strong>"
            f"<div class='muted'>{escape(str(lane.get('cohort_key') or ''))}</div></td>"
            f"<td><span class='pill'>{escape(tier)}</span></td>"
            f"<td>{'£'+format(stake,'.2f') if stake > 0 else '—'}</td>"
            f"<td>{int(lane.get('settled') or 0)}</td>"
            f"<td>{_fmt(lane.get('hit_rate_pct'),1,'%')}</td>"
            f"<td class='{_tone(lane.get('roi_pct'))}'>{_fmt(lane.get('roi_pct'),1,'%')}</td>"
            f"<td class='{_tone(lane.get('reference_pnl_gbp'))}'>{_money(lane.get('reference_pnl_gbp'))}</td>"
            f"<td>{escape(checkpoint.replace('_',' '))}</td>"
            "</tr>"
        )

    return f"""
    <section id='shadow'>
      <div class='section-title'>
        <div><h2>Frozen Shadow Portfolio</h2><div class='muted'>Rules frozen from {_start(state.get('portfolio_started_at'))}. Treating qualifying bets exactly as if they were being placed, without real-money execution.</div></div>
        <div><span class='pill ok'>{rules}</span> <span class='pill'>{authority}</span></div>
      </div>
      <div class='grid compact'>{metrics}</div>
      <div class='panel' style='margin-top:12px'>
        <div class='table-wrap'><table>
          <thead><tr><th>Lane</th><th>Tier</th><th>Stake</th><th>Settled</th><th>Hit rate</th><th>ROI</th><th>Nominal P&amp;L</th><th>Checkpoint</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table></div>
        <div class='muted compact-note' style='margin-top:10px'>Core £10 · challengers £5 · diagnostic/retired lanes £0 · duplicate selections are staked once. Interim review at 50 settled unique bets; first promotion decision at 100. Frozen rules are not changed because of interim results.</div>
        <div class='nav' style='margin-top:12px'><a href='/api/frozen-shadow-portfolio'>Raw portfolio state</a><a href='/outcome-edge'>Outcome Edge</a><a href='/predictive-football-pred4'>PRED4</a></div>
      </div>
    </section>
    """


def install(db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_PAGE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION, "dashboard_version": dashboard_layout_v2.VERSION}

    _ORIGINAL_PAGE = dashboard_layout_v2._page

    def page_with_frozen_shadow(db_arg, base_style: str) -> str:
        html = _ORIGINAL_PAGE(db_arg, base_style)
        section = _section(db_arg)
        marker = "<section id='leaderboard'>"
        if marker in html:
            html = html.replace(marker, section + marker, 1)
        else:
            html = html.replace("</body>", section + "</body>", 1)
        nav_marker = "<a href='#leaderboard'>Research Leaderboard</a>"
        if nav_marker in html:
            html = html.replace(nav_marker, "<a href='#shadow'>Frozen Shadow</a>" + nav_marker, 1)
        return html

    dashboard_layout_v2._page = page_with_frozen_shadow
    dashboard_layout_v2.VERSION = DASHBOARD_VERSION
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "dashboard_version": DASHBOARD_VERSION,
        "section": "Frozen Shadow Portfolio",
        "presentation_only": True,
    }
