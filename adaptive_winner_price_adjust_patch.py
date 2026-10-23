from __future__ import annotations

"""Price-adjust the W1 historical discovery leaderboard.

W1's probability model deliberately remains a winner-identification model: price
is not used to decide who is more likely to win.  This patch changes only the
*descriptive discovery ranking* shown to researchers.  Historical fingerprints
are ranked by actual hit rate minus the raw probability implied by the frozen
entry price, with sample-size shrinkage to reduce small-pocket noise.

Research/presentation only.  No selection, staking, card-construction, provider
polling, alerting or betting authority is changed.
"""

from collections import defaultdict
import re
from typing import Any, Dict

import adaptive_winner_research as core

VERSION = "ADAPTIVE_WINNER_PRICE_ADJUST_V1"
MIN_N = 30
SHRINK_N = 80.0
_INSTALLED = False
_ORIGINAL_WINNER_SCOREBOARD = None
_ORIGINAL_DASHBOARD_HTML = None


def _price_adjusted_discoveries(db, min_n: int = MIN_N, limit: int = 20):
    start = core._ensure_forward_start(db, "winner_discovery_state", "forward_start_at")
    rows = []
    for row in core._all_w1_candidates(db):
        created = core._dt(row.get("created_at"))
        odds = core._f(row.get("entry_odds"))
        result = str(row.get("result") or "").upper()
        if not created or created >= start or result not in {"WIN", "LOSS"}:
            continue
        if odds is None or odds <= 1.0:
            continue
        rows.append(row)

    stats: Dict[str, Dict[str, Dict[str, Any]]] = {
        key: defaultdict(lambda: {"n": 0, "wins": 0, "expected": 0.0, "pnl": 0.0, "pnl_n": 0})
        for key in core.W1_SEGMENTS
    }

    for row in rows:
        odds = float(row["entry_odds"])
        implied = min(0.99, max(0.01, 1.0 / odds))
        y = 1 if str(row.get("result") or "").upper() == "WIN" else 0
        pnl = core._f(row.get("pnl_units"))
        features = core._w1_feature_row(row)
        for key in core.W1_SEGMENTS:
            value = str(features.get(key) or "")
            if not value:
                continue
            s = stats[key][value]
            s["n"] += 1
            s["wins"] += y
            s["expected"] += implied
            if pnl is not None:
                s["pnl"] += float(pnl)
                s["pnl_n"] += 1

    discoveries = []
    for key, buckets in stats.items():
        for value, s in buckets.items():
            n = int(s["n"])
            if n < min_n:
                continue
            actual = float(s["wins"]) / n
            expected = float(s["expected"]) / n
            excess_pp = (actual - expected) * 100.0
            shrink = n / (n + SHRINK_N)
            adjusted_score = excess_pp * shrink
            roi = (float(s["pnl"]) / int(s["pnl_n"]) * 100.0) if int(s["pnl_n"]) else None
            discoveries.append({
                "feature": key,
                "value": value,
                "n": n,
                "wins": int(s["wins"]),
                "hit_rate_pct": actual * 100.0,
                "expected_hit_rate_pct": expected * 100.0,
                # Keep the legacy key so the existing table can render without
                # changing the core module.  Its meaning is now explicit below.
                "uplift_pp": excess_pp,
                "price_excess_pp": excess_pp,
                "price_adjusted_score_pp": adjusted_score,
                "realized_roi_pct": roi,
                "method": "actual_hit_rate_minus_raw_implied_probability",
            })

    return sorted(
        discoveries,
        key=lambda x: (x["price_adjusted_score_pp"], x["n"]),
        reverse=True,
    )[:limit]


def install(db) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_WINNER_SCOREBOARD, _ORIGINAL_DASHBOARD_HTML
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ORIGINAL_WINNER_SCOREBOARD = core.winner_scoreboard
    _ORIGINAL_DASHBOARD_HTML = core._dashboard_html

    def winner_scoreboard_price_adjusted(db_obj):
        state = dict(_ORIGINAL_WINNER_SCOREBOARD(db_obj))
        raw = list(state.get("historical_discoveries") or [])
        adjusted = _price_adjusted_discoveries(db_obj)
        state["raw_historical_discoveries"] = raw
        state["historical_discoveries"] = adjusted
        state["discovery_method"] = "actual_hit_rate_minus_raw_implied_probability"
        state["discovery_min_n"] = MIN_N
        state["discovery_shrink_n"] = SHRINK_N
        state["price_adjustment_note"] = (
            "Raw implied probability (1/entry_odds) is intentionally conservative because "
            "bookmaker margin is not removed when a complete contemporaneous market is unavailable."
        )
        return state

    def dashboard_html_price_adjusted(db_obj, base_style):
        html = _ORIGINAL_DASHBOARD_HTML(db_obj, base_style)
        html = html.replace(
            "Historical Winner Fingerprints",
            "Price-Adjusted Winner Fingerprints",
        )
        html = html.replace("<th>Uplift</th>", "<th>Excess vs price</th>", 1)
        html = html.replace("<th>Uplift pp</th>", "<th>Excess vs price</th>", 1)
        html = re.sub(
            r"(Price-Adjusted Winner Fingerprints[^<]*</h[23]>)",
            r"\1<div class='muted'>Ranked by actual hit rate minus the probability implied by the frozen entry price; small samples are shrunk in ranking.</div>",
            html,
            count=1,
        )
        return html

    core.winner_scoreboard = winner_scoreboard_price_adjusted
    core._dashboard_html = dashboard_html_price_adjusted

    preview = _price_adjusted_discoveries(db, limit=5)
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "method": "actual_hit_rate_minus_raw_implied_probability",
        "min_n": MIN_N,
        "shrink_n": SHRINK_N,
        "top_preview": preview,
        "winner_probability_model_changed": False,
        "betting_authority_changed": False,
    }
