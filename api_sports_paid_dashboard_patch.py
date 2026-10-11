from __future__ import annotations

"""Surface the paid American-football PRED1 lanes on the existing research page."""

import sports_predictive_dashboard as sports_dash
import betting_lab_research_clarity_patch as clarity
import web as web_module

_INSTALLED = False


def _lane_for_model(model_name: str) -> str:
    name = str(model_name or "").upper()
    if name.startswith("NFL"):
        return "NFL"
    if name.startswith("NCAAF") or name.startswith("NCAA"):
        return "NCAAF"
    return "EUROLEAGUE"


def install() -> dict:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True}

    models = list(sports_dash.MODELS)
    if not any(model == "NCAAF-PRED1" for model, _ in models):
        # Keep American-football lanes together on the page.
        out = []
        inserted = False
        for item in models:
            out.append(item)
            if item[0] == "NFL-PRED1":
                out.append(("NCAAF-PRED1", "NCAAF"))
                inserted = True
        if not inserted:
            out.append(("NCAAF-PRED1", "NCAAF"))
        sports_dash.MODELS = tuple(out)

    clarity._lane_for_model = _lane_for_model
    web_module.VERSION = "0.19.46"
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "models": [model for model, _ in sports_dash.MODELS],
        "dashboard_version": web_module.VERSION,
    }
