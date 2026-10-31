from __future__ import annotations

"""Canonical event-result fallback for Hit Rate Lab settlement.

HR cards historically depended on the original upstream shadow/execution bet row
being settled first. That coupling can leave a completed event PENDING inside HR
when the event result is already stored locally but the source bet missed its
settlement pass. This patch keeps the existing source-row result as first
priority, then resolves only from exact event_id matches in the established
local result tables.

Settlement-only: no candidate, pricing, card-construction, staking, alerting or
betting authority changes, and no provider calls are added.
"""

from collections import Counter
import re
import unicodedata
from typing import Any, Dict, Mapping, Optional, Tuple

import hit_rate_multiples_lab as hr

VERSION = "HR_SETTLEMENT_CANONICAL_FALLBACK_V1"
_INSTALLED = False
_ORIGINAL_SOURCE_RESULT = None
_STATS: Counter = Counter()


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _football_result(db, leg: Mapping[str, Any]) -> Optional[str]:
    event_id = str(leg.get("event_id") or "")
    if not event_id:
        return None
    try:
        row = db.fetchone(
            "SELECT home_score,away_score FROM event_results WHERE event_id=?",
            (event_id,),
        ) or {}
    except Exception:
        return None
    if row.get("home_score") is None or row.get("away_score") is None:
        return None

    home_score = int(row["home_score"])
    away_score = int(row["away_score"])
    market = str(leg.get("market_key") or "").strip().lower()
    selection = str(leg.get("selection") or "").strip()
    selected = _norm(selection)

    if market in {"h2h", "moneyline", "1x2"}:
        home = _norm(leg.get("home_team"))
        away = _norm(leg.get("away_team"))
        if selected in {"draw", "tie", "x"}:
            return "WIN" if home_score == away_score else "LOSS"
        if selected and selected == home:
            return "WIN" if home_score > away_score else "LOSS"
        if selected and selected == away:
            return "WIN" if away_score > home_score else "LOSS"
        return None

    if market in {"totals", "total", "over_under", "overunder"}:
        try:
            point = float(leg.get("point"))
        except Exception:
            return None
        total = float(home_score + away_score)
        if abs(total - point) <= 1e-12:
            return "PUSH"
        if selected.startswith("over"):
            return "WIN" if total > point else "LOSS"
        if selected.startswith("under"):
            return "WIN" if total < point else "LOSS"
        return None

    if market in {"btts", "both_teams_to_score", "bothteamstoscore"}:
        both = home_score > 0 and away_score > 0
        if selected in {"yes", "y", "true", "bttsyes"}:
            return "WIN" if both else "LOSS"
        if selected in {"no", "n", "false", "bttsno"}:
            return "WIN" if not both else "LOSS"
        return None

    return None


def _tennis_result(db, leg: Mapping[str, Any]) -> Optional[str]:
    event_id = str(leg.get("event_id") or "")
    if not event_id:
        return None
    try:
        row = db.fetchone(
            "SELECT winner FROM tennis_results WHERE event_id=?",
            (event_id,),
        ) or {}
    except Exception:
        return None
    winner = _norm(row.get("winner"))
    selection = _norm(leg.get("selection"))
    if not winner or not selection:
        return None
    return "WIN" if selection == winner else "LOSS"


def _multisport_result(db, leg: Mapping[str, Any]) -> Optional[str]:
    event_id = str(leg.get("event_id") or "")
    if not event_id:
        return None
    try:
        row = db.fetchone(
            "SELECT home_score,away_score,winner,result_kind FROM multisport_results WHERE event_id=?",
            (event_id,),
        ) or {}
    except Exception:
        return None
    if not row:
        return None

    market = str(leg.get("market_key") or "").strip().lower()
    if market not in {"h2h", "moneyline"}:
        return None

    kind = str(row.get("result_kind") or "").strip().upper()
    if kind in {"TIE", "PUSH"}:
        # MSP1 is a two-way market. A completed tied final is a push at the HR
        # research-system level; venue-specific dead-heat handling remains in
        # the upstream MSP settlement layer and is not guessed here.
        return "PUSH"

    winner = _norm(row.get("winner"))
    selection = _norm(leg.get("selection"))
    if winner and selection:
        return "WIN" if selection == winner else "LOSS"

    # Defensive score fallback for provider-completed rows whose winner field is
    # unexpectedly blank. Only exact event_id rows reach this branch.
    if row.get("home_score") is None or row.get("away_score") is None:
        return None
    hs = int(row["home_score"])
    aw = int(row["away_score"])
    if hs == aw:
        return "PUSH"
    home = _norm(leg.get("home_team"))
    away = _norm(leg.get("away_team"))
    if selection and selection == home:
        return "WIN" if hs > aw else "LOSS"
    if selection and selection == away:
        return "WIN" if aw > hs else "LOSS"
    return None


def canonical_leg_result(db, leg: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    table = str(leg.get("source_table") or "")
    if table == "execution_shadow_bets":
        result = _football_result(db, leg)
        return result, "event_results" if result else None
    if table == "tennis_execution_bets":
        result = _tennis_result(db, leg)
        return result, "tennis_results" if result else None
    if table == "multisport_execution_bets":
        result = _multisport_result(db, leg)
        return result, "multisport_results" if result else None
    return None, None


def _source_result_with_fallback(db, leg: Mapping[str, Any]) -> Optional[str]:
    if _ORIGINAL_SOURCE_RESULT is not None:
        result = _ORIGINAL_SOURCE_RESULT(db, leg)
        if result:
            return result

    result, source = canonical_leg_result(db, leg)
    if result:
        _STATS["fallback_resolutions"] += 1
        _STATS[f"source:{source}"] += 1
        _STATS[f"result:{result}"] += 1
    else:
        _STATS["fallback_misses"] += 1
    return result


def _pending_snapshot(db, limit: int = 12) -> Dict[str, Any]:
    try:
        pending = db.fetchall(
            """SELECT l.id,l.card_id,l.source_table,l.event_id,l.market_key,l.selection,
                      l.home_team,l.away_team,l.commence_time
               FROM hit_rate_legs l
               JOIN hit_rate_cards c ON c.id=l.card_id
               WHERE c.status!='SETTLED' AND l.result IS NULL
               ORDER BY l.commence_time,l.id"""
        )
    except Exception:
        pending = []

    by_source = Counter(str(x.get("source_table") or "UNKNOWN") for x in pending)
    resolvable = Counter()
    examples = []
    for leg in pending:
        result, source = canonical_leg_result(db, leg)
        if result:
            resolvable[source or "UNKNOWN"] += 1
            if len(examples) < limit:
                examples.append({
                    "card_id": int(leg.get("card_id") or 0),
                    "event_id": str(leg.get("event_id") or ""),
                    "fixture": f"{leg.get('home_team') or ''} v {leg.get('away_team') or ''}".strip(),
                    "selection": str(leg.get("selection") or ""),
                    "canonical_result": result,
                    "canonical_source": source,
                })
    return {
        "pending_legs": len(pending),
        "pending_by_source": dict(by_source),
        "canonically_resolvable": int(sum(resolvable.values())),
        "resolvable_by_table": dict(resolvable),
        "examples": examples,
    }


def install(db=None) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SOURCE_RESULT
    if not _INSTALLED:
        current = getattr(hr, "_source_result", None)
        if current is not _source_result_with_fallback:
            _ORIGINAL_SOURCE_RESULT = current
            hr._source_result = _source_result_with_fallback
        _INSTALLED = True

    before = _pending_snapshot(db) if db is not None else None
    repair = hr.settle_cards(db) if db is not None else {"touched": 0, "settled": 0}
    after = _pending_snapshot(db) if db is not None else None
    state = {
        "installed": True,
        "version": VERSION,
        "exact_event_id_only": True,
        "canonical_tables": ["event_results", "tennis_results", "multisport_results"],
        "source_row_first": True,
        "startup_repair": repair,
        "before": before,
        "after": after,
        "fallback_stats": dict(_STATS),
        "provider_calls_added": 0,
        "selection_rules_changed": False,
        "pricing_rules_changed": False,
        "card_rules_changed": False,
        "staking_changed": False,
        "alerting_changed": False,
        "betting_authority_changed": False,
    }
    print(f"HR_SETTLEMENT_FALLBACK_INSTALL {state}", flush=True)
    return state
