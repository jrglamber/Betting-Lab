from __future__ import annotations

"""Shared Betting Lab presentation for the rolling HR research family.

Reuses web.BASE_STYLE rather than creating detached unstyled HTML pages.
Presentation only: all research queries, decisions, exports and historical
results stay in the original engines.
"""

from html import escape
from typing import Any, Iterable

import web as web_module


EXTRA_STYLE = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{max-width:1600px;margin:0 auto}
.lab-nav{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:14px 0 22px}
.lab-nav a{background:#161b22;border:1px solid #30363d;border-radius:8px;
 padding:10px 13px;color:#c9d1d9;font-size:13px;font-weight:600}
.lab-nav a:hover,.lab-nav a:focus-visible{border-color:#58a6ff;color:#f0f6fc}
.lab-nav a[aria-current=page]{background:#203a55;border-color:#388bfd;color:#fff}
.lab-eyebrow{color:#8b949e;text-transform:uppercase;font-size:11px;letter-spacing:.1em;
 margin:14px 0 6px;font-weight:700}
.lab-desc{color:#8b949e;font-size:14px;line-height:1.6;max-width:1000px;margin:4px 0 18px}
.lab-summary{background:#161b22;border:1px solid #30363d;border-radius:10px;
 padding:14px 17px;margin-bottom:20px;line-height:1.6;color:#c9d1d9}
.lab-summary strong{color:#e6edf3}
.lab-metrics{grid-template-columns:repeat(auto-fit,minmax(165px,1fr))}
.lab-metrics .value{font-size:clamp(18px,2.7vw,27px);overflow-wrap:anywhere}
.lab-panel-heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px;
 flex-wrap:wrap;margin-bottom:12px}
.lab-panel-heading h2{font-size:18px;margin:0}
.lab-panel-heading .muted{font-size:12px;line-height:1.5}
.lab-empty{text-align:center;padding:22px 10px;color:#8b949e;font-size:14px}
.lab-table-wrap{max-width:100%;overflow:auto}
.lab-table-wrap table{width:100%;min-width:620px}
.lab-table-wrap th{white-space:nowrap}
.lab-table-wrap td{overflow-wrap:anywhere;line-height:1.45;max-width:330px}
.lab-foot{color:#8b949e;font-size:12px;line-height:1.6;padding:0 0 18px}
.lab-status{display:inline-block;padding:3px 9px;background:#21262d;
 border:1px solid #30363d;border-radius:20px;font-size:11px;color:#c9d1d9}
@media(max-width:650px){
 body{padding:16px}
 .lab-nav a{flex:1;text-align:center;font-size:12px;padding:11px 7px}
 .lab-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}
 .panel{padding:14px}
 .lab-table-wrap table{min-width:580px}
 h1{font-size:23px;line-height:1.3}
}
"""


def _t(value: Any) -> str:
    return escape(str(value if value is not None else "—"), quote=True)


def _num(value: Any, digits: int = 1, *, signed=False, suffix="") -> str:
    if value is None:
        return "—"
    try:
        n = float(value)
        return f"{n:+.{digits}f}{suffix}" if signed else f"{n:.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _int(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return str(int(value))
    except (ValueError, TypeError):
        return "—"


def nav(active: str) -> str:
    links = [
        ("hit-rate-multiples", "/hit-rate-multiples", "Hit-Rate Multiples"),
        ("hr-portfolio-shadow", "/hr-portfolio-shadow", "Portfolio Shadow"),
        ("hr-quality-challenger", "/hr-quality-challenger", "Quality Challenger"),
    ]
    return "<nav class='lab-nav' aria-label='Hit-Rate research navigation'>" + "".join(
        f"<a href='{href}'" +
        (" aria-current='page'" if active == key else "") +
        f">{escape(label)}</a>"
        for key, href, label in links
    ) + "</nav>"


def page_open(title: str, *, active: str, heading: str, sub: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_t(title)} | Betting Lab</title>"
        f"<style>{web_module.BASE_STYLE}\n{EXTRA_STYLE}</style>"
        "</head><body>"
        "<div class='lab-eyebrow'>Betting Lab / Hit-Rate Research</div>"
        f"<h1>{_t(heading)} <span class='pill'>SHADOW · FORWARD ONLY</span></h1>"
        f"{nav(active)}"
        f"<p class='lab-desc'>{_t(sub)}</p>"
    )


def card(label: str, value: Any, detail: str = "") -> str:
    return (
        f"<div class='card'><div class='label'>{_t(label)}</div>"
        f"<div class='value'>{_t(value)}</div>"
        + (f"<div class='muted' style='font-size:12px;margin-top:8px'>{_t(detail)}</div>" if detail else "")
        + "</div>"
    )


def table(headers: Iterable[str], rows: Iterable[Iterable[Any]], empty: str) -> str:
    headers = list(headers)
    rows = list(rows)
    cells = "".join("<tr>" + "".join(f"<td>{_t(v)}</td>" for v in row) + "</tr>" for row in rows)
    if not cells:
        cells = f"<tr><td class='lab-empty' colspan='{len(headers)}'>{_t(empty)}</td></tr>"
    return (
        "<div class='lab-table-wrap'><table><thead><tr>" +
        "".join(f"<th scope='col'>{_t(x)}</th>" for x in headers) +
        "</tr></thead><tbody>" + cells + "</tbody></table></div>"
    )


def panel(title: str, *, note: str = "", contents: str = "") -> str:
    return (
        "<section class='panel'><div class='lab-panel-heading'>"
        f"<h2>{_t(title)}</h2>" +
        (f"<span class='muted'>{_t(note)}</span>" if note else "") +
        "</div>" + contents + "</section>"
    )


def main_hr_quicklinks(html: str) -> str:
    """Place navigation immediately below HR's heading; never append to footer."""
    marker = "<div class='sub'>"
    if "aria-label='Hit-Rate research navigation'" in html:
        return html
    if marker not in html:
        return html
    # BASE_STYLE already provides the existing dark panel/button tokens.
    nav_html = (
        "<nav aria-label='Hit-Rate research navigation' "
        "class='panel' style='display:flex;align-items:center;gap:8px;"
        "flex-wrap:wrap;padding:12px;margin:15px 0 16px'>"
        "<a class='button secondary' style='margin:0' "
        "href='/hr-portfolio-shadow'>Portfolio Shadow</a>"
        "<a class='button secondary' style='margin:0' "
        "href='/hr-quality-challenger'>Quality Challenger</a>"
        "<span class='muted' style='font-size:12px'>Research only · no live bets</span>"
        "</nav>"
    )
    return html.replace(marker, nav_html + marker, 1)


def render_portfolio(s: dict[str, Any]) -> str:
    portfolio = next(
        (p for p in s.get("portfolio", []) if p.get("cohort") == "UNIFIED"),
        (s.get("portfolio") or [None])[0],
    )
    n = _int(portfolio.get("settled_groups")) if portfolio else "0"
    money = _num(portfolio.get("portfolio_pnl_units"), 2, signed=True, suffix="u") if portfolio else "—"
    roi = _num(portfolio.get("portfolio_roi_pct"), 1, signed=True, suffix="%") if portfolio else "—"
    dd = _num(portfolio.get("max_drawdown_units"), 2, suffix="u") if portfolio else "—"
    mix = s.get("recent_ncaaf_mix") or {}
    body = page_open(
        "HR Portfolio Shadow", active="hr-portfolio-shadow",
        heading="HR Portfolio Shadow",
        sub="One rolling pool of qualifying selections. Compare doubles, Trixies and Yankees "
            "against singles without forcing a particular sport or waiting for future fixtures.",
    )
    body += (
        "<div class='grid lab-metrics'>" +
        card("Settled groups", n, "Forward only") +
        card("Portfolio P/L", money, "At £10 total reference stake per group") +
        card("Portfolio ROI", roi, "Not a prediction of future returns") +
        card("Maximum drawdown", dd, "Settled group sequence") +
        "</div>"
        "<div class='lab-summary'><strong>Construction:</strong> chronological baseline · "
        "William Hill or Ladbrokes only · coherent frozen prices · 2–4 legs. "
        "NCAAF 3.00–4.99 has no mandatory quota. "
        "<strong>Research only:</strong> no automated bets or alerts."
        "</div>"
    )
    body += panel(
        "Portfolio results", note="Same £10 total reference stake per group",
        contents=table(
            ["Pool", "Settled groups", "P/L", "ROI", "Max drawdown"],
            [
                [p.get("cohort"), _int(p.get("settled_groups")),
                 _num(p.get("portfolio_pnl_units"), 2, signed=True, suffix="u"),
                 _num(p.get("portfolio_roi_pct"), 1, signed=True, suffix="%"),
                 _num(p.get("max_drawdown_units"), 2, suffix="u")]
                for p in s.get("portfolio", [])
            ],
            "Awaiting forward-settled portfolios",
        ),
    )
    body += panel(
        "NCAAF contribution", note="Latest 25 group formations",
        contents="<div class='grid lab-metrics'>" +
        card("No NCAAF", _int(mix.get("zero", 0)), "Groups with 0 NCAAF legs") +
        card("Mixed", _int(mix.get("some", 0)), "At least one NCAAF and one other") +
        card("All NCAAF", _int(mix.get("all", 0)), "Every leg from NCAAF") +
        "</div>",
    )
    body += panel(
        "Format benchmarks", note="Same frozen selection set",
        contents=table(
            ["Pool", "Format", "Settled", "Normalised P/L", "ROI"],
            [[r.get("cohort"), r.get("format"), _int(r.get("settled")),
              _num(r.get("normalized_pnl_units"), 2, signed=True, suffix="u"),
              _num(r.get("roi_pct"), 1, signed=True, suffix="%")]
             for r in s.get("formats", [])],
            "No settled comparisons yet",
        ),
    )
    body += panel(
        "Per-lane forward evidence",
        note="Distinct selections, not duplicated multiple legs",
        contents=table(
            ["Source lane", "Selections", "Settled", "Hit rate", "Singles ROI"],
            [[r.get("source_lane"), _int(r.get("unique_selections")),
              _int(r.get("settled")),
              _num(r.get("hit_rate_pct"), 1, suffix="%"),
              _num(r.get("singles_roi_pct"), 1, signed=True, suffix="%")]
             for r in s.get("lane_metrics", [])],
            "Awaiting qualifying selections",
        ),
    )
    body += panel(
        "Source lane eligibility",
        note="WATCH / PAUSED lanes do not enter new shadow groups",
        contents=table(
            ["Lane", "Eligibility", "Review rationale"],
            [[r.get("source_lane"), r.get("status"), r.get("review_note")]
             for r in s.get("lane_policy", [])],
            "No reviewed lanes yet",
        ),
    )
    body += panel(
        "Recent portfolio groups",
        note="Most recent 25 · source mix is not forced",
        contents=table(
            ["Group", "Pool", "Bookmaker", "Legs", "NCAAF legs", "State"],
            [[_int(r.get("id")), r.get("cohort"), r.get("bookmaker_key"),
              _int(r.get("selection_count")), _int(r.get("ncaaf_legs")),
              r.get("status")]
             for r in s.get("latest_groups", [])],
            "No qualifying same-book groups yet",
        ),
    )
    body += (
        "<div class='lab-foot'>Forward recording began " + _t(s.get("started_at")) +
        ". Historical research remains separately archived. "
        "<a href='/api/hr-portfolio-shadow/status'>Data &amp; source details (JSON)</a>. "
        "Descriptive return is not proof of betting edge.</div></body></html>"
    )
    return body


def render_quality(s: dict[str, Any]) -> str:
    paired = int(s.get("paired") or 0)
    body = page_open(
        "Rolling Quality Challenger", active="hr-quality-challenger",
        heading="Rolling Quality Challenger",
        sub="A matched forward test of choosing the strongest currently available "
            "price-quality signals against the chronological first-available baseline.",
    )
    body += (
        "<div class='grid lab-metrics'>" +
        card("Settled paired groups", paired, "Both approaches must settle") +
        card("Chronological P/L",
             _num(s.get("baseline_pnl_units"), 2, signed=True, suffix="u") if paired else "—",
             "Matched decisions only") +
        card("Ranked P/L",
             _num(s.get("quality_pnl_units"), 2, signed=True, suffix="u") if paired else "—",
             "Matched decisions only") +
        card("Ranked vs baseline",
             _num(s.get("paired_delta_pnl_units"), 2, signed=True, suffix="u") if paired else "—",
             "Positive means the challenger leads") +
        "</div>"
        "<div class='lab-summary'><strong>Fair comparison:</strong> same bookmaker, quote "
        "window, decision time, number of legs and £10 total stake. "
        "<strong>Initial experimental rank:</strong> 55% price premium above frozen "
        "minimum · 35% quote freshness · 10% bookmaker availability. "
        "This is a heuristic, not a calibrated win-probability or EV model.</div>"
    )
    body += panel(
        "Paired decision health",
        note="No retrospective re-selection",
        contents="<div class='grid lab-metrics'>" +
        card("Awaiting settlement", _int(s.get("pending")), "Open comparisons") +
        card("Ranked ahead", _int(s.get("quality_wins")), "Finished groups") +
        card("Baseline ahead", _int(s.get("baseline_wins")), "Finished groups") +
        card("Identical result", _int(s.get("same_result")), "Finished groups") +
        "</div>",
    )
    body += panel(
        "Per-format matched results",
        note="Same format allocation within each paired group",
        contents=table(
            ["Format", "Settled", "Chronological P/L", "Ranked P/L", "Difference"],
            [[r.get("format"), _int(r.get("settled")),
              _num(r.get("baseline_pnl_units"), 2, signed=True, suffix="u"),
              _num(r.get("quality_pnl_units"), 2, signed=True, suffix="u"),
              _num(r.get("delta_pnl_units"), 2, signed=True, suffix="u")]
             for r in s.get("formats", [])],
            "No completed paired comparisons yet",
        ),
    )
    body += panel(
        "Recent ranking decisions",
        note="Matches a previously formed chronological group",
        contents=table(
            ["Group", "Book", "Pool size", "Overlap", "NCAAF (baseline/ranked)", "Status", "P/L delta"],
            [[_int(r.get("baseline_group_id")), r.get("bookmaker_key"),
              _int(r.get("candidate_count")), _int(r.get("overlap_count")),
              f"{_int(r.get('baseline_ncaaf_count'))} / {_int(r.get('quality_ncaaf_count'))}",
              r.get("status"), _num(r.get("delta_pnl_units"), 2, signed=True, suffix="u")]
             for r in s.get("latest", [])],
            "Awaiting new eligible decisions",
        ),
    )
    body += panel(
        "Skipped comparisons",
        note="Exclusions remain in export for review",
        contents=table(
            ["Reason", "Count"],
            [[reason, _int(count)] for reason, count in sorted((s.get("skipped") or {}).items())],
            "No skipped comparisons",
        ),
    )
    body += (
        "<div class='lab-foot'>Forward start: " + _t(s.get("started_at")) +
        ". No additional odds API calls or live execution. "
        "Results are correlated because the same fixtures may recur across cards. "
        "<a href='/api/hr-quality-challenger/status'>Detailed decision evidence (JSON)</a>."
        "</div></body></html>"
    )
    return body
