from __future__ import annotations

from statistics import mean
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


def implied_probability(decimal_odds: float) -> float:
    if decimal_odds <= 1.0:
        raise ValueError("decimal odds must be > 1.0")
    return 1.0 / decimal_odds


def devig_prices(prices: Mapping[str, float]) -> Dict[str, float]:
    raw = {name: implied_probability(float(price)) for name, price in prices.items()}
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("invalid overround")
    return {name: prob / total for name, prob in raw.items()}


def fair_odds(probability: float) -> float:
    if not 0 < probability < 1:
        raise ValueError("probability must be between 0 and 1")
    return 1.0 / probability


def expected_value(probability: float, decimal_odds: float) -> float:
    return probability * decimal_odds - 1.0


def edge_pct(probability: float, decimal_odds: float) -> float:
    return expected_value(probability, decimal_odds) * 100.0


def min_odds_for_probability(probability: float, min_edge_pct: float = 0.0) -> float:
    target_return = 1.0 + (min_edge_pct / 100.0)
    return target_return / probability


def consensus_probabilities(
    bookmaker_prices: Mapping[str, Mapping[str, float]],
    *,
    exclude_bookmaker: Optional[str] = None,
    min_books: int = 3,
) -> Optional[Dict[str, float]]:
    probs_by_selection: Dict[str, List[float]] = {}
    books_used = 0

    for bookmaker, prices in bookmaker_prices.items():
        if exclude_bookmaker and bookmaker == exclude_bookmaker:
            continue
        if len(prices) < 2:
            continue
        try:
            devigged = devig_prices(prices)
        except (ValueError, ZeroDivisionError):
            continue
        books_used += 1
        for selection, prob in devigged.items():
            probs_by_selection.setdefault(selection, []).append(prob)

    if books_used < min_books:
        return None

    result = {
        selection: mean(values)
        for selection, values in probs_by_selection.items()
        if values
    }
    total = sum(result.values())
    if total <= 0:
        return None
    return {selection: prob / total for selection, prob in result.items()}


def clv_pct(offered_odds: float, closing_odds: float) -> float:
    if offered_odds <= 1.0 or closing_odds <= 1.0:
        raise ValueError("odds must be > 1.0")
    # Positive means we took a bigger price than the closing market price.
    return (offered_odds / closing_odds - 1.0) * 100.0
