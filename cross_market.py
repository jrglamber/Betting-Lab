from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class ScoreModel:
    lambda_home: float
    lambda_away: float
    home_win: float
    draw: float
    away_win: float
    over_25: float
    btts_yes: float
    home_dnb: float
    away_dnb: float
    loss: float


def _poisson(k: int, lam: float) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def score_market_probabilities(
    lambda_home: float,
    lambda_away: float,
    max_goals: int = 10,
) -> Dict[str, float]:
    home = [_poisson(i, lambda_home) for i in range(max_goals + 1)]
    away = [_poisson(i, lambda_away) for i in range(max_goals + 1)]

    p_home = p_draw = p_away = p_over25 = p_btts = 0.0
    total_mass = 0.0
    for hg, hp in enumerate(home):
        for ag, ap in enumerate(away):
            p = hp * ap
            total_mass += p
            if hg > ag:
                p_home += p
            elif hg == ag:
                p_draw += p
            else:
                p_away += p
            if hg + ag >= 3:
                p_over25 += p
            if hg >= 1 and ag >= 1:
                p_btts += p

    if total_mass <= 0:
        raise ValueError("invalid poisson mass")

    p_home /= total_mass
    p_draw /= total_mass
    p_away /= total_mass
    p_over25 /= total_mass
    p_btts /= total_mass

    non_draw = max(1e-12, 1.0 - p_draw)
    return {
        "home": p_home,
        "draw": p_draw,
        "away": p_away,
        "over_25": p_over25,
        "btts_yes": p_btts,
        "home_dnb": p_home / non_draw,
        "away_dnb": p_away / non_draw,
    }


def fit_score_model(
    home_win: float,
    draw: float,
    away_win: float,
    over_25: float,
    *,
    min_lambda: float = 0.15,
    max_lambda: float = 4.50,
    step: float = 0.05,
) -> ScoreModel:
    targets = (home_win, draw, away_win, over_25)
    if any(not 0 < x < 1 for x in targets):
        raise ValueError("all market probabilities must be in (0,1)")

    best = None
    h = min_lambda
    while h <= max_lambda + 1e-9:
        a = min_lambda
        while a <= max_lambda + 1e-9:
            p = score_market_probabilities(h, a)
            # Give the 1X2 distribution and O/U signal similar aggregate weight.
            loss = (
                (p["home"] - home_win) ** 2
                + (p["draw"] - draw) ** 2
                + (p["away"] - away_win) ** 2
                + 1.5 * (p["over_25"] - over_25) ** 2
            )
            if best is None or loss < best[0]:
                best = (loss, h, a, p)
            a += step
        h += step

    assert best is not None
    loss, h, a, p = best
    return ScoreModel(
        lambda_home=h,
        lambda_away=a,
        home_win=p["home"],
        draw=p["draw"],
        away_win=p["away"],
        over_25=p["over_25"],
        btts_yes=p["btts_yes"],
        home_dnb=p["home_dnb"],
        away_dnb=p["away_dnb"],
        loss=loss,
    )
