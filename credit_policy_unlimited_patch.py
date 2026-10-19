from __future__ import annotations

"""Targeted provider-credit policy for quote-refresh research lanes.

The provider's real remaining-credit guard remains authoritative, but the two
high-frequency manual-price labs also get explicit daily budgets so a busy slate
cannot consume the monthly allowance disproportionately.

Live Yankee keeps the adaptive failed-price backoff introduced in V2. HR now
adds card-feasibility and readiness pacing: it does not spend quote credits on a
stream until there are enough distinct events to form the requested system, it
polls distant non-near-card events more slowly, and it preserves five-minute
refreshes when a same-book card is genuinely close to completion or kickoff is
near. Candidate generation, modelling, settlement, card rules, staking and
execution authority are unchanged.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Sequence

import hit_rate_multiples_lab
import live_yankee_feed

VERSION = "CREDIT_POLICY_TARGETED_BUDGET_V4"
HIT_RATE_DAILY_CREDITS = 750
LIVE_YANKEE_DAILY_CREDITS = 1_500
BASE_REFRESH_MINUTES = 5.0
MID_BACKOFF_MINUTES = 20.0
FAR_BACKOFF_MINUTES = 30.0
CLOSE_TO_KICKOFF_MINUTES = 90.0
FAR_FROM_KICKOFF_MINUTES = 240.0
RECENT_POLL_WINDOW_MINUTES = 30.0
REPEATED_MISS_POLLS = 2

# HR-specific pacing. A stream is only worth paid polling once it has enough
# distinct events to form its requested multiple. Distant streams get periodic
# snapshots rather than continuous five-minute refreshes. If one bookmaker is
# already one leg short of a complete card, normal five-minute polling resumes.
HR_MID_REFRESH_MINUTES = 20.0
HR_FAR_REFRESH_MINUTES = 60.0
HR_NEAR_CARD_MISSING_LEGS = 1

_INSTALLED = False
_ORIGINAL_LY_TARGETS = None
_ORIGINAL_HRM_TARGETS = None


def _dt(value: Any):
    return live_yankee_feed._parse_iso(value)


def _recent_successes(db, run_type: str, event_id: str, now: datetime):
    since = (now - timedelta(minutes=RECENT_POLL_WINDOW_MINUTES)).isoformat()
    try:
        return db.fetchall(
            """
            SELECT started_at FROM collector_runs
            WHERE run_type=? AND event_id=? AND ok=1 AND started_at>=?
            ORDER BY started_at DESC,id DESC LIMIT 3
            """,
            (run_type, event_id, since),
        )
    except Exception:
        return []


def _last_success(db, run_type: str, event_id: str):
    try:
        row = db.fetchone(
            """SELECT MAX(started_at) AS at FROM collector_runs
               WHERE run_type=? AND event_id=? AND ok=1""",
            (run_type, event_id),
        ) or {}
        return _dt(row.get("at"))
    except Exception:
        return None


def _live_yankee_has_usable(db, event_id: str, now: datetime) -> bool:
    try:
        rows = db.fetchall(
            "SELECT * FROM live_yankee_candidates WHERE event_id=? AND status='QUEUED' ORDER BY id",
            (event_id,),
        )
        return any(bool(live_yankee_feed._valid_quotes(db, row, now)) for row in rows)
    except Exception:
        # Fail open: an observability/backoff failure must never suppress research.
        return True


def _backoff_targets(
    db,
    now: datetime,
    targets: Sequence[Mapping[str, Any]],
    run_type: str,
    has_usable: Callable[[Any, str, datetime], bool],
):
    kept = []
    for raw in targets:
        target = dict(raw)
        event_id = str(target.get("event_id") or "")
        kickoff = _dt(target.get("commence_time"))
        if not event_id or kickoff is None:
            kept.append(raw)
            continue

        minutes_to_kickoff = (kickoff - now).total_seconds() / 60.0
        if minutes_to_kickoff <= CLOSE_TO_KICKOFF_MINUTES:
            kept.append(raw)
            continue

        # If a currently usable quote exists, preserve the normal five-minute
        # cadence so a nearly placeable Live Yankee card is not starved.
        if has_usable(db, event_id, now):
            kept.append(raw)
            continue

        recent = _recent_successes(db, run_type, event_id, now)
        if len(recent) < REPEATED_MISS_POLLS:
            kept.append(raw)
            continue

        required = (
            FAR_BACKOFF_MINUTES
            if minutes_to_kickoff > FAR_FROM_KICKOFF_MINUTES
            else MID_BACKOFF_MINUTES
        )
        last = _dt((recent[0] or {}).get("started_at")) if recent else None
        if last is None:
            kept.append(raw)
            continue
        elapsed = (now - last).total_seconds() / 60.0
        if elapsed + 1e-9 >= required:
            kept.append(raw)
    return kept


def _hr_stream_readiness(db, now: datetime) -> tuple[set[str], set[str], Dict[str, int]]:
    """Return HR events worth polling and events in near-complete same-book streams."""
    try:
        rows = [dict(x) for x in db.fetchall(
            "SELECT * FROM hit_rate_candidates WHERE status='QUEUED' ORDER BY first_seen_at,id"
        )]
    except Exception:
        # Fail open to the original target list if diagnostics cannot be read.
        return set(), set(), {"eligible_streams": 0, "near_streams": 0, "failed_open": 1}

    streams: Dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if hit_rate_multiples_lab._actionable(row.get("commence_time"), now):
            streams[(
                str(row.get("policy_version") or ""),
                str(row.get("experiment_key") or ""),
                str(row.get("system_type") or ""),
            )].append(row)

    eligible_events: set[str] = set()
    near_events: set[str] = set()
    eligible_streams = near_streams = 0

    for (_, _, system), candidates in streams.items():
        spec = hit_rate_multiples_lab.SYSTEMS.get(system) or {}
        need = int(spec.get("legs") or 0)
        if need <= 0:
            continue

        event_ids = {str(c.get("event_id") or "") for c in candidates if str(c.get("event_id") or "")}
        if len(event_ids) < need:
            # No amount of quote refreshing can create this system yet.
            continue

        eligible_streams += 1
        eligible_events.update(event_ids)

        # Measure current same-book overlap by distinct events. Multiple
        # candidate rows from one event can never count as multiple legs.
        book_events: Dict[str, set[str]] = defaultdict(set)
        for candidate in candidates:
            event_id = str(candidate.get("event_id") or "")
            if not event_id:
                continue
            try:
                quotes = hit_rate_multiples_lab._quotes(db, candidate, now, False)
            except Exception:
                quotes = {}
            for book in quotes:
                if str(book):
                    book_events[str(book)].add(event_id)

        best_overlap = max((len(events) for events in book_events.values()), default=0)
        if best_overlap >= max(1, need - HR_NEAR_CARD_MISSING_LEGS):
            near_streams += 1
            near_events.update(event_ids)

    return eligible_events, near_events, {
        "eligible_streams": eligible_streams,
        "near_streams": near_streams,
        "failed_open": 0,
    }


def _hit_rate_targets_with_pacing(db, now: datetime, targets: Sequence[Mapping[str, Any]]):
    eligible_events, near_events, state = _hr_stream_readiness(db, now)
    if state.get("failed_open"):
        return list(targets)

    kept = []
    for raw in targets:
        target = dict(raw)
        event_id = str(target.get("event_id") or "")
        kickoff = _dt(target.get("commence_time"))
        if not event_id or kickoff is None:
            continue
        if event_id not in eligible_events:
            continue

        # Once a same-book card is one leg short, keep the original five-minute
        # reaction cadence so a short-lived price window is not missed.
        if event_id in near_events:
            kept.append(raw)
            continue

        minutes_to_kickoff = (kickoff - now).total_seconds() / 60.0
        if minutes_to_kickoff <= CLOSE_TO_KICKOFF_MINUTES:
            kept.append(raw)
            continue

        required = (
            HR_FAR_REFRESH_MINUTES
            if minutes_to_kickoff > FAR_FROM_KICKOFF_MINUTES
            else HR_MID_REFRESH_MINUTES
        )
        last = _last_success(db, "HIT_RATE_MULTIPLES_QUOTES", event_id)
        if last is None:
            kept.append(raw)
            continue
        elapsed = (now - last).total_seconds() / 60.0
        if elapsed + 1e-9 >= required:
            kept.append(raw)

    return kept


def install() -> dict:
    global _INSTALLED, _ORIGINAL_LY_TARGETS, _ORIGINAL_HRM_TARGETS

    before = {
        "hit_rate_multiples": int(hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET),
        "live_yankee": int(live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET),
    }

    hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET = HIT_RATE_DAILY_CREDITS
    live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET = LIVE_YANKEE_DAILY_CREDITS

    if not _INSTALLED:
        _ORIGINAL_LY_TARGETS = live_yankee_feed._targets
        _ORIGINAL_HRM_TARGETS = hit_rate_multiples_lab._targets

        def live_yankee_targets_with_backoff(db, now=None):
            now = now or datetime.now(timezone.utc)
            targets = _ORIGINAL_LY_TARGETS(db, now)
            return _backoff_targets(
                db, now, targets, "LIVE_YANKEE_QUOTES", _live_yankee_has_usable
            )

        def hit_rate_targets_with_pacing(db, now=None):
            now = now or datetime.now(timezone.utc)
            targets = _ORIGINAL_HRM_TARGETS(db, now)
            return _hit_rate_targets_with_pacing(db, now, targets)

        live_yankee_feed._targets = live_yankee_targets_with_backoff
        hit_rate_multiples_lab._targets = hit_rate_targets_with_pacing

    result = {
        "installed": True,
        "already_installed": _INSTALLED,
        "version": VERSION,
        "policy": "targeted_daily_caps_plus_card_readiness_pacing",
        "before": before,
        "after": {
            "hit_rate_multiples": int(hit_rate_multiples_lab.DAILY_REFRESH_CREDIT_BUDGET),
            "live_yankee": int(live_yankee_feed.DAILY_REFRESH_CREDIT_BUDGET),
        },
        "live_yankee_backoff": {
            "base_minutes": BASE_REFRESH_MINUTES,
            "after_repeated_no_usable_quote": REPEATED_MISS_POLLS,
            "mid_minutes": MID_BACKOFF_MINUTES,
            "far_minutes": FAR_BACKOFF_MINUTES,
            "close_to_kickoff_minutes": CLOSE_TO_KICKOFF_MINUTES,
            "far_from_kickoff_minutes": FAR_FROM_KICKOFF_MINUTES,
            "recent_poll_window_minutes": RECENT_POLL_WINDOW_MINUTES,
        },
        "hit_rate_pacing": {
            "daily_credits": HIT_RATE_DAILY_CREDITS,
            "requires_full_candidate_count": True,
            "near_card_missing_legs": HR_NEAR_CARD_MISSING_LEGS,
            "near_card_refresh_minutes": BASE_REFRESH_MINUTES,
            "mid_refresh_minutes": HR_MID_REFRESH_MINUTES,
            "far_refresh_minutes": HR_FAR_REFRESH_MINUTES,
            "close_to_kickoff_minutes": CLOSE_TO_KICKOFF_MINUTES,
            "far_from_kickoff_minutes": FAR_FROM_KICKOFF_MINUTES,
        },
        "selection_rules_changed": False,
        "betting_authority_changed": False,
    }
    _INSTALLED = True
    return result
