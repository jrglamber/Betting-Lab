from __future__ import annotations

"""Repair API-Sports PRED1 settlement across provider naming conventions.

NCAAF forecasts use API-Sports school names, while price-linked shadow bets can
store the matched Odds-API team name including its mascot.  The legacy settle
path compared those two labels literally, so winning NCAAF bets could be marked
LOSS.  The matched Odds-API event already gives us an unambiguous home/away
side, so settle/reconciliation should compare sides rather than provider labels.

Research accounting only: no forecast, edge, price, staking or execution rule is
changed and no external provider call is made.
"""

from typing import Any, Dict, Mapping, Optional

import api_sports_predictive as core

VERSION = "APISPORTS_SETTLEMENT_SIDE_V1"
_INSTALLED = False
_ORIGINAL_SETTLE = None


def _selection_side(db, bet: Mapping[str, Any], forecast: Mapping[str, Any]) -> Optional[str]:
    event_id = str(bet.get("odds_event_id") or forecast.get("odds_event_id") or "").strip()
    selection = str(bet.get("selection") or "").strip()
    if event_id and selection:
        event = db.fetchone(
            "SELECT home_team,away_team FROM multisport_events WHERE event_id=?",
            (event_id,),
        ) or {}
        if selection == str(event.get("home_team") or ""):
            return "HOME"
        if selection == str(event.get("away_team") or ""):
            return "AWAY"

    # Safe fallback for rows created before the Odds-API linkage patch or rows
    # whose selection happened to use the provider label directly.
    if selection == str(forecast.get("home_team") or ""):
        return "HOME"
    if selection == str(forecast.get("away_team") or ""):
        return "AWAY"
    return None


def reconcile_ncaaf(db) -> Dict[str, Any]:
    rows = db.fetchall(
        """
        SELECT b.*,f.home_team AS forecast_home_team,f.away_team AS forecast_away_team,
               f.home_score AS final_home,f.away_score AS final_away,
               f.odds_event_id AS forecast_odds_event_id,f.status AS forecast_status
        FROM api_sports_pred_shadow_bets b
        JOIN api_sports_pred_forecasts f ON f.id=b.forecast_id
        WHERE b.model_name='NCAAF-PRED1'
          AND b.status='SETTLED'
          AND f.status='SETTLED'
        ORDER BY b.id
        """
    )
    checked = corrected = unresolved = wins = losses = 0
    pnl = 0.0
    corrections = []
    for raw in rows:
        b = dict(raw)
        forecast = {
            "home_team": b.get("forecast_home_team"),
            "away_team": b.get("forecast_away_team"),
            "odds_event_id": b.get("forecast_odds_event_id"),
        }
        side = _selection_side(db, b, forecast)
        hs = core._safe_float(b.get("final_home"))
        aas = core._safe_float(b.get("final_away"))
        if side is None or hs is None or aas is None or hs == aas:
            unresolved += 1
            continue
        checked += 1
        won = (side == "HOME" and hs > aas) or (side == "AWAY" and aas > hs)
        result = "WIN" if won else "LOSS"
        correct_pnl = float(b.get("offered_odds") or 0.0) - 1.0 if won else -1.0
        if str(b.get("result") or "") != result or abs(float(b.get("pnl_units") or 0.0) - correct_pnl) > 1e-9:
            db.execute(
                "UPDATE api_sports_pred_shadow_bets SET result=?,pnl_units=? WHERE id=? AND status='SETTLED'",
                (result, correct_pnl, int(b["id"])),
            )
            corrected += 1
            if len(corrections) < 12:
                corrections.append({
                    "bet_id": int(b["id"]),
                    "selection": str(b.get("selection") or ""),
                    "side": side,
                    "old_result": str(b.get("result") or ""),
                    "new_result": result,
                    "offered_odds": float(b.get("offered_odds") or 0.0),
                })
        wins += int(won)
        losses += int(not won)
        pnl += correct_pnl

    return {
        "version": VERSION,
        "model": "NCAAF-PRED1",
        "settled_rows": len(rows),
        "side_resolved": checked,
        "unresolved": unresolved,
        "corrected": corrected,
        "wins": wins,
        "losses": losses,
        "pnl_units": pnl,
        "roi_pct": (pnl / checked * 100.0) if checked else None,
        "sample_corrections": corrections,
        "provider_calls": 0,
        "model_changed": False,
        "pricing_rules_changed": False,
        "betting_authority": False,
    }


def _settle_with_side_reconciliation(db, lane):
    out = dict(_ORIGINAL_SETTLE(db, lane))
    if getattr(lane, "key", "") == "NCAAF":
        audit = reconcile_ncaaf(db)
        out.update({
            "settlement_side_version": VERSION,
            "settlement_reconciled": int(audit.get("corrected") or 0),
            "settled_wins": int(audit.get("wins") or 0),
            "settled_losses": int(audit.get("losses") or 0),
            "settled_pnl_units": audit.get("pnl_units"),
            "settlement_unresolved": int(audit.get("unresolved") or 0),
        })
    return out


def install() -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_SETTLE
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    _ORIGINAL_SETTLE = core.settle
    core.settle = _settle_with_side_reconciliation
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "policy": "settle NCAAF shadow bets by matched Odds-API home/away side",
        "provider_calls": 0,
        "model_changed": False,
        "pricing_rules_changed": False,
        "betting_authority": False,
    }
