from __future__ import annotations

from html import escape
import betting_lab_research_clarity_patch as clarity
import web as web_module

_INSTALLED=False
_ORIGINAL_NOTICE=None


def _fmt(value,digits=3):
    if value is None: return "—"
    try: return f"{float(value):.{digits}f}"
    except Exception: return escape(str(value))


def _notice():
    base=_ORIGINAL_NOTICE()
    try:
        rows=web_module.db.fetchall("SELECT * FROM api_sports_pred_walkforward_summary ORDER BY lane_key")
    except Exception:
        rows=[]
    if not rows:
        return base
    body="".join(
        f"<tr><td><strong>{escape(str(r.get('lane_key') or ''))}</strong></td>"
        f"<td>{int(r.get('samples') or 0)}</td>"
        f"<td>{_fmt(r.get('avg_brier'))}</td>"
        f"<td>{_fmt(r.get('baseline_brier'))}</td>"
        f"<td>{_fmt(r.get('brier_improvement_pct'),2)}%</td>"
        f"<td>{_fmt(r.get('accuracy_pct'),2)}%</td></tr>" for r in rows
    )
    panel=(
        "<div class='panel priority'><h2>Historical walk-forward · PRED1</h2>"
        "<div class='muted'>Strict chronological evaluation: each historical game is forecast using only earlier games. "
        "Games sharing a kickoff time are predicted before any same-time result updates the model. "
        "There are no historical bookmaker prices in this feed, so this tests prediction quality rather than historical ROI.</div>"
        "<div class='table-wrap'><table><thead><tr><th>Lane</th><th>Forecasts</th><th>Brier</th>"
        "<th>Prior home-rate baseline</th><th>Brier improvement</th><th>Direction accuracy</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div></div>"
    )
    return base+panel


def install():
    global _INSTALLED,_ORIGINAL_NOTICE
    if _INSTALLED: return {'installed':True,'already_installed':True}
    _ORIGINAL_NOTICE=clarity._provider_notice
    clarity._provider_notice=_notice
    web_module.VERSION='0.19.43'
    _INSTALLED=True
    return {'installed':True,'already_installed':False,'dashboard_version':web_module.VERSION}
