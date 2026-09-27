from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Iterable, Optional


@dataclass(frozen=True)
class SlowBookSignal:
    offered_odds: float
    market_median_odds: float
    relative_price_gap_pct: float


def detect_slow_book(
    target_odds: float,
    peer_odds: Iterable[float],
    *,
    minimum_gap_pct: float = 4.0,
) -> Optional[SlowBookSignal]:
    peers = [float(x) for x in peer_odds if float(x) > 1.0]
    if len(peers) < 2 or target_odds <= 1.0:
        return None
    med = median(peers)
    gap = (float(target_odds) / med - 1.0) * 100.0
    if gap < minimum_gap_pct:
        return None
    return SlowBookSignal(float(target_odds), med, gap)
