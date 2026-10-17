from __future__ import annotations

"""Surface cumulative settled P/L on the Live Yankee and HR1/HR2 dashboards.

Presentation/accounting only.  No selection, formation, quote, Telegram or
settlement rules are changed.  Placeable totals exclude CROSS_BOOK_SYNTHETIC
cards so the displayed running P/L represents cards that could actually have
been placed at one bookmaker under the current execution policy.
"""

from typing import Any, Dict

import hit_rate_multiples_lab as hr
import live_yankee_feed as live

VERSION = "CUMULATIVE_PNL_DASHBOARD_V1"
_INSTALLED = False
_ORIGINAL_LIVE_PAGE = None
_ORIGINAL_LIVE_SCOREBOARD = None
_ORIGINAL_HR_PAGE = None
_ORIGINAL_HR_SCOREBOARD = None


def _money(value: Any) -> str:
    try:
        x = float(value or 0.0)
    except Exception:
        x = 0.0
    if abs(x) < 0.005:
        return "£0.00"
    return f"£{x:+,.2f}"


def _tone(value: Any) -> str:
    try:
        x = float(value or 0.0)
    except Exception:
        return ""
    if x > 0.004:
        return "ok"
    if x < -0.004:
        return "bad"
    return ""


def _summarise(rows) -> Dict[str, Any]:
    settled = [dict(x) for x in rows if x.get("reference_pnl_gbp") is not None]
    pnl = sum(float(x.get("reference_pnl_gbp") or 0.0) for x in settled)
    stake = sum(float(x.get("reference_stake_gbp") or 10.0) for x in settled)
    return {
        "settled": len(settled),
        "pnl_gbp": pnl,
        "stake_gbp": stake,
        "roi_pct": (pnl / stake * 100.0) if stake > 0 else None,
    }


def _live_totals(db) -> Dict[str, Any]:
    try:
        rows = [dict(x) for x in db.fetchall(
            "SELECT id,assembly_mode,status,reference_stake_gbp,reference_pnl_gbp "
            "FROM live_yankee_cards WHERE status='SETTLED' ORDER BY id"
        )]
    except Exception:
        rows = []
    placeable = [x for x in rows if str(x.get("assembly_mode") or "") != "CROSS_BOOK_SYNTHETIC"]
    synthetic = [x for x in rows if str(x.get("assembly_mode") or "") == "CROSS_BOOK_SYNTHETIC"]
    out = _summarise(placeable)
    synth = _summarise(synthetic)
    out.update({
        "synthetic_settled": synth["settled"],
        "synthetic_pnl_gbp": synth["pnl_gbp"],
    })
    return out


def _hr_totals(db) -> Dict[str, Any]:
    try:
        rows = [dict(x) for x in db.fetchall(
            "SELECT id,policy_version,experiment_key,system_type,assembly_mode,status,"
            "reference_stake_gbp,reference_pnl_gbp "
            "FROM hit_rate_cards WHERE status='SETTLED' ORDER BY id"
        )]
    except Exception:
        rows = []
    placeable = [x for x in rows if str(x.get("assembly_mode") or "") != "CROSS_BOOK_SYNTHETIC"]
    synthetic = [x for x in rows if str(x.get("assembly_mode") or "") == "CROSS_BOOK_SYNTHETIC"]
    streams: Dict[str, Dict[str, Any]] = {}
    for exp in ("HR1", "HR2"):
        for system in ("YANKEE", "HEINZ"):
            stream_rows = [
                x for x in placeable
                if str(x.get("experiment_key") or "") == exp
                and str(x.get("system_type") or "") == system
            ]
            streams[f"{exp}_{system}"] = _summarise(stream_rows)
    overall = _summarise(placeable)
    synth = _summarise(synthetic)
    return {
        "overall": overall,
        "streams": streams,
        "synthetic_settled": synth["settled"],
        "synthetic_pnl_gbp": synth["pnl_gbp"],
    }


def _metric(label: str, value: str, note: str = "", css: str = "") -> str:
    return (
        "<div class='card'><div class='label'>" + label + "</div>"
        f"<div class='value {css}'>{value}</div>"
        f"<div class='muted'>{note}</div></div>"
    )


def _live_panel(db) -> str:
    t = _live_totals(db)
    roi = "—" if t["roi_pct"] is None else f"{float(t['roi_pct']):+.1f}%"
    cards = "".join((
        _metric("CUMULATIVE P/L", _money(t["pnl_gbp"]), "settled placeable cards", _tone(t["pnl_gbp"])),
        _metric("SETTLED CARDS", f"{int(t['settled'])}", "same-book live-feed cards"),
        _metric("REFERENCE STAKE", f"£{float(t['stake_gbp']):,.2f}", "£10 total stake per settled card"),
        _metric("CUMULATIVE ROI", roi, "P/L ÷ reference stake", _tone(t["pnl_gbp"])),
    ))
    synth_note = ""
    if int(t["synthetic_settled"]):
        synth_note = (
            f" Cross-book synthetic research is excluded: {int(t['synthetic_settled'])} settled card(s), "
            f"{_money(t['synthetic_pnl_gbp'])} research P/L."
        )
    return (
        "<div class='panel' id='live-cumulative-pnl'><h2>Running cumulative P/L</h2>"
        f"<div class='grid compact'>{cards}</div>"
        "<div class='muted' style='margin-top:10px'>Updates from settled cards on every page load. "
        "Only genuinely placeable same-book cards count toward the headline total."
        f"{synth_note}</div></div>"
    )


def _hr_panel(db) -> str:
    t = _hr_totals(db)
    overall = t["overall"]
    roi = "—" if overall["roi_pct"] is None else f"{float(overall['roi_pct']):+.1f}%"
    top = "".join((
        _metric("TOTAL PLACEABLE P/L", _money(overall["pnl_gbp"]), "HR1 + HR2 · all forward policy versions", _tone(overall["pnl_gbp"])),
        _metric("SETTLED PLACEABLE", f"{int(overall['settled'])}", "Yankee + Heinz cards"),
        _metric("REFERENCE STAKE", f"£{float(overall['stake_gbp']):,.2f}", "£10 total stake per card"),
        _metric("CUMULATIVE ROI", roi, "placeable P/L ÷ stake", _tone(overall["pnl_gbp"])),
    ))
    stream_cards = []
    for exp in ("HR1", "HR2"):
        for system in ("YANKEE", "HEINZ"):
            s = t["streams"][f"{exp}_{system}"]
            stream_cards.append(_metric(
                f"{exp} {system}",
                _money(s["pnl_gbp"]),
                f"{int(s['settled'])} settled placeable card(s)",
                _tone(s["pnl_gbp"]),
            ))
    synth_note = ""
    if int(t["synthetic_settled"]):
        synth_note = (
            f" The historical cross-book synthetic cohort remains visible as research evidence but is excluded "
            f"from these totals: {int(t['synthetic_settled'])} settled synthetic card(s), "
            f"{_money(t['synthetic_pnl_gbp'])} research-only P/L."
        )
    return (
        "<div class='panel' id='hr-cumulative-pnl'><h2>Running cumulative P/L</h2>"
        f"<div class='grid compact'>{top}</div>"
        "<div class='muted' style='margin:12px 0 8px'>By independent forward stream:</div>"
        f"<div class='grid compact'>{''.join(stream_cards)}</div>"
        "<div class='muted' style='margin-top:10px'>Settled placeable same-book cards only; totals continue across future HR policy versions rather than resetting."
        f"{synth_note}</div></div>"
    )


def _live_page() -> str:
    html = _ORIGINAL_LIVE_PAGE()
    if "id='live-cumulative-pnl'" in html:
        return html
    panel = _live_panel(live.web_module.db)
    marker = "<div class='panel'><h2>Execution model</h2>"
    if marker in html:
        return html.replace(marker, panel + marker, 1)
    return html.replace("</body>", panel + "</body>", 1)


def _hr_page(web_module) -> str:
    html = _ORIGINAL_HR_PAGE(web_module)
    if "id='hr-cumulative-pnl'" in html:
        return html
    panel = _hr_panel(web_module.db)
    marker = "<div class='panel'><h2>Forward streams</h2>"
    if marker in html:
        return html.replace(marker, panel + marker, 1)
    return html.replace("</body>", panel + "</body>", 1)


def _live_scoreboard(db) -> Dict[str, Any]:
    out = dict(_ORIGINAL_LIVE_SCOREBOARD(db))
    t = _live_totals(db)
    out["cumulative_pnl"] = {
        "placeable_pnl_gbp": t["pnl_gbp"],
        "placeable_settled_cards": t["settled"],
        "placeable_reference_stake_gbp": t["stake_gbp"],
        "placeable_roi_pct": t["roi_pct"],
        "synthetic_excluded_cards": t["synthetic_settled"],
        "synthetic_excluded_pnl_gbp": t["synthetic_pnl_gbp"],
    }
    return out


def _hr_scoreboard(db) -> Dict[str, Any]:
    out = dict(_ORIGINAL_HR_SCOREBOARD(db))
    out["cumulative_pnl"] = _hr_totals(db)
    return out


def install(db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_LIVE_PAGE, _ORIGINAL_LIVE_SCOREBOARD
    global _ORIGINAL_HR_PAGE, _ORIGINAL_HR_SCOREBOARD
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_LIVE_PAGE = live._page_html
    _ORIGINAL_LIVE_SCOREBOARD = live.scoreboard
    _ORIGINAL_HR_PAGE = hr._page
    _ORIGINAL_HR_SCOREBOARD = hr.scoreboard

    live._page_html = _live_page
    live.scoreboard = _live_scoreboard
    hr._page = _hr_page
    hr.scoreboard = _hr_scoreboard
    _INSTALLED = True

    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "live_yankee": _live_totals(db),
        "hit_rate": _hr_totals(db),
        "synthetic_policy": "excluded_from_placeable_cumulative_pnl",
    }
