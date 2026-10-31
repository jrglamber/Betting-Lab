from __future__ import annotations

"""Expose the forward HR2 Double/Trixie challengers on the main Multiples table.

The curated homepage predates the small-system experiment and hard-codes the
HR1/HR2 Yankee + Heinz rows.  This presentation-only patch keeps that renderer
intact and inserts the two active HR2 small-system rows from the same canonical
hit-rate tables.  Rows are shown even before a card has settled so the homepage
always reflects the experiments that are actually running.
"""

from html import escape
from typing import Any, Dict

import dashboard_layout_v2
import hit_rate_multiples_lab as hr

VERSION = "MULTIPLES_HOME_HR2_SMALL_SYSTEMS_V1"
_INSTALLED = False
_ORIGINAL_PAGE = None


def _fmt_roi(value: Any) -> str:
    try:
        if value is None:
            return "—"
        return f"{float(value):.1f}%"
    except Exception:
        return "—"


def _metrics(db, system: str) -> Dict[str, Any]:
    try:
        return dict(hr._metrics(db, "HR2", system) or {})
    except Exception:
        return {}


def _row(db, system: str, label: str) -> str:
    m = _metrics(db, system)
    cards = int(m.get("cards") or 0)
    settled = int(m.get("settled") or 0)
    queued = int(m.get("queued") or 0)
    roi = _fmt_roi(m.get("roi_pct"))
    return (
        "<tr><td><a href='/hit-rate-multiples'><strong>"
        + escape(label)
        + "</strong></a></td><td>"
        + str(cards)
        + "</td><td>"
        + str(settled)
        + "</td><td>"
        + roi
        + "</td><td class='muted'>queue "
        + str(queued)
        + " · forward research</td></tr>"
    )


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_PAGE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_PAGE = dashboard_layout_v2._page

    def _page_with_small_systems(db, base_style: str) -> str:
        html = _ORIGINAL_PAGE(db, base_style)
        # Do not duplicate rows if a future base dashboard adds them natively.
        if "<strong>HR2 Double</strong>" in html or "<strong>HR2 Trixie</strong>" in html:
            return html

        marker = "<tr><td><a href='/hit-rate-multiples'><strong>HR2 Heinz</strong>"
        pos = html.find(marker)
        if pos < 0:
            # Fail open: never damage the homepage if the base markup changes.
            return html

        extra = _row(db, "DOUBLE", "HR2 Double") + _row(db, "TRIXIE", "HR2 Trixie")
        return html[:pos] + extra + html[pos:]

    dashboard_layout_v2._page = _page_with_small_systems
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "rows": ["HR2 Double", "HR2 Trixie"],
        "source": "canonical hit_rate_cards metrics",
        "presentation_only": True,
        "selection_rules_changed": False,
        "provider_calls_added": 0,
        "betting_authority_changed": False,
    }
