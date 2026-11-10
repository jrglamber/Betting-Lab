from __future__ import annotations

"""Read-only HR2 quote eligibility audit. No provider calls, changes, or betting."""

from collections import Counter
from datetime import datetime, timedelta, timezone

import credit_policy_unlimited_patch as base_policy
import hit_rate_multiples_lab as hr


def snapshot(db, now=None):
    now = now or datetime.now(timezone.utc)
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    rows = [dict(x) for x in db.fetchall(
        """SELECT id,policy_version,experiment_key,system_type,source_lane,
                  event_id,sport_key,league,home_team,away_team,commence_time
           FROM hit_rate_candidates WHERE status='QUEUED'
           ORDER BY commence_time,id"""
    )]
    relevant = [
        x for x in rows
        if x.get("policy_version") == hr.POLICY_VERSION
        and x.get("experiment_key") == "HR2"
        and x.get("system_type") == "DOUBLE"
    ]
    events = {}
    for x in relevant:
        eid = str(x.get("event_id") or "")
        if not eid or eid in events:
            continue
        dt = hr._parse(x.get("commence_time"))
        if dt is None:
            gate = "INVALID_KICKOFF"
            hours = None
        else:
            hours = round((dt - now).total_seconds() / 3600, 2)
            gate = ("EXPIRED" if dt <= now else
                    "OUTSIDE_30_HOUR_WINDOW" if dt > now + timedelta(hours=hr.FORMATION_HORIZON_HOURS)
                    else "WITHIN_30_HOUR_WINDOW")
        events[eid] = {
            "event_id": eid,
            "sport": x.get("sport_key"),
            "fixture": f"{x.get('home_team') or '?'} v {x.get('away_team') or '?'}",
            "kickoff_utc": x.get("commence_time"),
            "hours_to_kickoff": hours,
            "gate": gate,
        }
    sources = Counter(str(x.get("source_lane") or "") for x in relevant)
    gates = Counter(x["gate"] for x in events.values())
    natural = getattr(base_policy, "_ORIGINAL_HRM_TARGETS", None)
    raw_due = list(natural(db, now)) if callable(natural) else None
    live_due = list(hr._targets(db, now))
    out = {
        "utc": now.isoformat(),
        "policy": hr.POLICY_VERSION,
        "queued_hr2_double_rows": len(relevant),
        "distinct_hr2_double_fixtures": len(events),
        "within_30h": gates.get("WITHIN_30_HOUR_WINDOW", 0),
        "outside_30h": gates.get("OUTSIDE_30_HOUR_WINDOW", 0),
        "expired": gates.get("EXPIRED", 0),
        "invalid_kickoff": gates.get("INVALID_KICKOFF", 0),
        "source_lanes": dict(sources),
        "base_due_events": None if raw_due is None else len({str(x.get("event_id") or "") for x in raw_due}),
        "actual_due_events": len({str(x.get("event_id") or "") for x in live_due}),
        "frozen_hr_credits_today": int(hr._credits_today(db, now)),
        "runtime_hr_daily_cap": int(hr.DAILY_REFRESH_CREDIT_BUDGET),
        "upcoming_events": list(events.values())[:20],
        "no_provider_calls": True,
        "research_and_betting_rules_unchanged": True,
    }
    out["diagnosis"] = (
        "FEWER_THAN_TWO_WITHIN_HORIZON" if out["within_30h"] < 2 else
        "NATIVE_DUE_FILTER_EMPTY" if out["base_due_events"] == 0 else
        "PACING_OR_BACKOFF" if out["actual_due_events"] == 0 else
        "ELIGIBLE_TO_POLL"
    )
    return out
