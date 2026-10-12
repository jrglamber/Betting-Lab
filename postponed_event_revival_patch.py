from __future__ import annotations

"""Allow provider-rescheduled football events to re-enter normal collection.

Historical rescue marks the *original* postponed event obligation as POSTPONED
so stale bets/signals cannot be graded with a fabricated 0-0.  The core event
upsert intentionally preserves an existing status on conflict, which means a
provider that keeps the same event_id but later changes its kickoff time would
otherwise leave that genuine rescheduled fixture stuck POSTPONED forever.

This wrapper only revives an event when all of the following are true:
- it was already POSTPONED before this discovery call;
- the provider returned the same event again in this discovery call; and
- its commence_time actually changed to a future time.

No selection, pricing, staking, settlement or execution rules are changed.
"""

from datetime import datetime, timezone
from typing import Any, Dict

VERSION = "POSTPONED_EVENT_REVIVAL_V1"
_INSTALLED = False


def _parse(value):
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if out.tzinfo is None:
            out = out.replace(tzinfo=timezone.utc)
        return out.astimezone(timezone.utc)
    except Exception:
        return None


def install(web_module) -> Dict[str, Any]:
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    collector = web_module.collector
    original = collector.discover

    def discover_with_postponed_revival():
        before = {}
        try:
            rows = web_module.db.fetchall(
                "SELECT event_id,commence_time FROM events WHERE status='POSTPONED'"
            )
            before = {str(r.get("event_id")): str(r.get("commence_time") or "") for r in rows}
        except Exception:
            before = {}

        result = original()
        now = datetime.now(timezone.utc)
        revived = []
        for event_id, old_commence in before.items():
            try:
                row = web_module.db.fetchone(
                    "SELECT event_id,commence_time,status FROM events WHERE event_id=?",
                    (event_id,),
                ) or {}
                new_commence = str(row.get("commence_time") or "")
                kickoff = _parse(new_commence)
                if (
                    str(row.get("status") or "") == "POSTPONED"
                    and new_commence
                    and new_commence != old_commence
                    and kickoff is not None
                    and kickoff > now
                ):
                    web_module.db.execute(
                        "UPDATE events SET status='UPCOMING' WHERE event_id=? AND status='POSTPONED'",
                        (event_id,),
                    )
                    revived.append({
                        "event_id": event_id,
                        "old_commence_time": old_commence,
                        "new_commence_time": new_commence,
                    })
            except Exception:
                continue
        if revived:
            print(f"POSTPONED_EVENT_REVIVAL {revived}", flush=True)
        return result

    collector.discover = discover_with_postponed_revival
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "rule": "same event_id + changed future commence_time only",
    }
