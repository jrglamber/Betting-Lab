from __future__ import annotations

"""Side-safe settlement for API-Sports PRED1 across provider naming conventions.

API-Sports supplies the forecast/result side while price-linked shadow bets can
store the matched Odds-API team label. Those labels need not be text-identical
(e.g. a school-only label versus a school + mascot label). The matched Odds-API
event already supplies an unambiguous home/away side, so reconciliation grades
by side rather than by cross-provider string equality.

Research accounting only: no forecast, edge, price, staking or execution rule is
changed and no external provider call is made.
"""

from typing import Any, Dict, Mapping, Optional, Sequence

import api_sports_predictive as core

VERSION = "APISPORTS_SETTLEMENT_SIDE_V2"
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

    # Safe fallback for legacy/direct rows whose selection used the API-Sports
    # provider label rather than the matched Odds-API label.
    if selection == str(forecast.get("home_team") or ""):
        return "HOME"
    if selection == str(forecast.get("away_team") or ""):
        return "AWAY"
    return None


def reconcile_model(db, model_name: str) -> Dict[str, Any]:
    model_name = str(model_name or "").strip()
    rows = db.fetchall(
        """
        SELECT b.*,f.home_team AS forecast_home_team,f.away_team AS forecast_away_team,
               f.home_score AS final_home,f.away_score AS final_away,
               f.odds_event_id AS forecast_odds_event_id,f.status AS forecast_status
        FROM api_sports_pred_shadow_bets b
        JOIN api_sports_pred_forecasts f ON f.id=b.forecast_id
        WHERE b.model_name=?
          AND b.status='SETTLED'
          AND f.status='SETTLED'
        ORDER BY b.id
        """,
        (model_name,),
    )
    checked = corrected = unresolved = wins = losses = ties = 0
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
        if hs is not None and aas is not None and hs == aas:
            # Preserve the pre-existing tie semantics rather than silently
            # inventing a push rule here. This patch addresses name mismatches.
            ties += 1
            continue
        if side is None or hs is None or aas is None:
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
        "model": model_name,
        "settled_rows": len(rows),
        "side_resolved": checked,
        "unresolved": unresolved,
        "ties_preserved": ties,
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


def reconcile_ncaaf(db) -> Dict[str, Any]:
    """Backward-compatible helper used by the dashboard bootstrap."""
    return reconcile_model(db, "NCAAF-PRED1")


def reconcile_all(db, model_names: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    if model_names is None:
        rows = db.fetchall(
            "SELECT DISTINCT model_name FROM api_sports_pred_shadow_bets ORDER BY model_name"
        )
        model_names = [str(row.get("model_name") or "") for row in rows if row.get("model_name")]
    audits = {name: reconcile_model(db, name) for name in model_names}
    return {
        "version": VERSION,
        "models": audits,
        "settled_rows": sum(int(x.get("settled_rows") or 0) for x in audits.values()),
        "side_resolved": sum(int(x.get("side_resolved") or 0) for x in audits.values()),
        "corrected": sum(int(x.get("corrected") or 0) for x in audits.values()),
        "unresolved": sum(int(x.get("unresolved") or 0) for x in audits.values()),
        "ties_preserved": sum(int(x.get("ties_preserved") or 0) for x in audits.values()),
        "provider_calls": 0,
        "model_changed": False,
        "pricing_rules_changed": False,
        "betting_authority": False,
    }


def _settle_with_side_reconciliation(db, lane):
    out = dict(_ORIGINAL_SETTLE(db, lane))
    model_name = str(getattr(lane, "model_name", "") or "").strip()
    if model_name:
        audit = reconcile_model(db, model_name)
        out.update({
            "settlement_side_version": VERSION,
            "settlement_reconciled": int(audit.get("corrected") or 0),
            "settled_wins": int(audit.get("wins") or 0),
            "settled_losses": int(audit.get("losses") or 0),
            "settled_pnl_units": audit.get("pnl_units"),
            "settlement_unresolved": int(audit.get("unresolved") or 0),
            "settlement_ties_preserved": int(audit.get("ties_preserved") or 0),
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
        "policy": "settle all API-Sports PRED1 shadow bets by matched Odds-API home/away side",
        "provider_calls": 0,
        "model_changed": False,
        "pricing_rules_changed": False,
        "betting_authority": False,
    }
