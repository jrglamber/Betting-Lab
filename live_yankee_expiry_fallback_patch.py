from __future__ import annotations

"""Expiry rescue for the shadow Live Yankee Feed.

Normal priority remains Yankee. If fewer than four price-ready unique events are
available and the earliest ready leg reaches the final 10 minutes before kickoff,
form a Trixie from three or a Double from two rather than waste the selections.
Used legs are consumed exactly once. Research/manual alerts only; no bet placement.
"""

from datetime import timedelta
from math import prod
import hashlib
import json
from typing import Any, Dict, Mapping, Sequence

import live_yankee_feed as feed
import live_yankee_consolidation_patch as consolidation
import web as web_module
from db import sanitize_sensitive_text, utc_now_iso
from telegram_alerts import _send_telegram

VERSION = "YANKEE_LIVE_FEED_V3_EXPIRY_FALLBACK"
FALLBACK_TRIGGER_MINUTES = 10.0
REFERENCE_TOTAL_STAKE_GBP = 10.0
SYSTEM_LINES = {"DOUBLE": 1, "TRIXIE": 4, "YANKEE": 11}

_INSTALLED = False
_ORIGINAL_EXPIRE = None
_ORIGINAL_FORM = None
_ORIGINAL_MESSAGE = None
_ORIGINAL_SCOREBOARD = None
_ORIGINAL_PAGE = None
_ORIGINAL_TARGETS = None


def _ensure_schema(db) -> None:
    feed._ensure_schema(db)
    for sql in (
        "ALTER TABLE live_yankee_cards ADD COLUMN system_type TEXT",
        "ALTER TABLE live_yankee_cards ADD COLUMN formation_reason TEXT",
    ):
        try:
            db.execute(sql)
        except Exception:
            pass
    try:
        db.execute("UPDATE live_yankee_cards SET system_type='YANKEE' WHERE system_type IS NULL OR system_type='' ")
    except Exception:
        pass


def _targets(db, now):
    normal = _ORIGINAL_TARGETS(db, now)
    if normal:
        return normal

    # The original refresher waits for four events. A fallback needs live prices
    # while only two or three legs exist, so keep those queues refreshed too.
    rows = db.fetchall("SELECT * FROM live_yankee_candidates WHERE status='QUEUED' ORDER BY first_seen_at,id")
    by_event: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not feed._actionable(row.get("commence_time"), now):
            continue
        eid = str(row.get("event_id") or "")
        if not eid:
            continue
        item = by_event.setdefault(eid, {
            "event_id": eid,
            "sport_key": str(row.get("sport_key") or ""),
            "commence_time": str(row.get("commence_time") or ""),
            "first_seen_at": str(row.get("first_seen_at") or ""),
            "markets": set(),
        })
        item["markets"].add(str(row.get("market_key") or "h2h"))
    if len(by_event) < 2:
        return []

    due = []
    for item in by_event.values():
        last = db.fetchone(
            "SELECT MAX(started_at) AS at FROM collector_runs WHERE run_type='LIVE_YANKEE_QUOTES' AND event_id=? AND ok=1",
            (item["event_id"],),
        ) or {}
        last_dt = feed._parse_iso(last.get("at"))
        if last_dt and (now - last_dt).total_seconds() / 60.0 < feed.QUOTE_REFRESH_MINUTES:
            continue
        item["markets"] = tuple(sorted(item["markets"]))
        due.append(item)
    return sorted(due, key=lambda x: (x["first_seen_at"], x["commence_time"], x["event_id"]))


def _ready(db, now):
    out = []
    seen_events = set()
    for raw in db.fetchall("SELECT * FROM live_yankee_candidates WHERE status='QUEUED' ORDER BY first_seen_at,id"):
        c = dict(raw)
        eid = str(c.get("event_id") or "")
        if not eid or eid in seen_events:
            continue
        kickoff = feed._parse_iso(c.get("commence_time"))
        if kickoff is None or kickoff <= now or kickoff > now + timedelta(hours=feed.FORMATION_HORIZON_HOURS):
            continue
        quotes = feed._valid_quotes(db, c, now)
        if not quotes:
            continue
        out.append((c, quotes, kickoff))
        seen_events.add(eid)
    return out


def _choose_quotes(db, candidates, quote_maps, now):
    common = set(quote_maps[0])
    for q in quote_maps[1:]:
        common.intersection_update(q)
    if common:
        book = max(common, key=lambda b: (prod(float(q[b]["price"]) for q in quote_maps), b))
        return [dict(q[book]) for q in quote_maps], "SAME_BOOK_VERIFIED"

    synthetic = [dict(max(q.values(), key=lambda x: (float(x["price"]), str(x.get("bookmaker_key") or "")))) for q in quote_maps]
    synthetic_prices = [float(q["price"]) for q in synthetic]
    synthetic_product = prod(synthetic_prices)
    anchor_counts: Dict[str, int] = {}
    for quote in synthetic:
        book = str(quote.get("bookmaker_key") or "")
        if book:
            anchor_counts[book] = anchor_counts.get(book, 0) + 1

    min_anchor = 1 if len(candidates) == 2 else 2
    options = []
    for book, anchor_count in anchor_counts.items():
        if anchor_count < min_anchor:
            continue
        proposed = []
        retentions = []
        valid = True
        for candidate, best_map, best_price in zip(candidates, quote_maps, synthetic_prices):
            qualified = best_map.get(book)
            alt = dict(qualified) if qualified is not None else consolidation._fresh_quote_for_book(db, candidate, book, now)
            if alt is None:
                valid = False
                break
            alt_price = float(alt.get("price") or 0)
            if alt_price <= 1 or best_price <= 1:
                valid = False
                break
            price_retention = alt_price / best_price
            min_odds = float(candidate.get("min_odds") or 0)
            required_retention = alt_price / min_odds if min_odds > 1 else 1.0
            if price_retention + 1e-12 < consolidation.MIN_LEG_PRICE_RETENTION:
                valid = False
                break
            if required_retention + 1e-12 < consolidation.MIN_REQUIRED_ODDS_RETENTION:
                valid = False
                break
            proposed.append(alt)
            retentions.append(price_retention)
        if not valid or len(proposed) != len(candidates):
            continue
        combined = prod(float(q["price"]) for q in proposed) / synthetic_product
        if combined + 1e-12 < consolidation.MIN_CARD_PRICE_RETENTION:
            continue
        options.append((anchor_count, combined, min(retentions), book, proposed))

    if options:
        options.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
        return options[0][4], "SAME_BOOK_CONSOLIDATED"
    return synthetic, "CROSS_BOOK_SYNTHETIC"


def _create_card(db, candidates, quotes, system_type: str, mode: str, reason: str):
    ids = [int(x["id"]) for x in candidates]
    key_raw = VERSION + "|" + system_type + "|" + reason + "|" + "|".join(map(str, ids))
    card_key = hashlib.sha256(key_raw.encode()).hexdigest()
    source_mix = ",".join(sorted({str(x.get("source_lane") or "") for x in candidates}))
    book_mix = ",".join(sorted({str(x.get("bookmaker_key") or "") for x in quotes}))
    oldest = max(float(x.get("quote_age_minutes") or 0) for x in quotes)
    db.execute(
        """INSERT INTO live_yankee_cards(
             card_key,created_at,algorithm_version,status,assembly_mode,source_mix,bookmaker_mix,
             oldest_quote_age_minutes,telegram_status,system_type,formation_reason
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(card_key) DO NOTHING""",
        (card_key, utc_now_iso(), VERSION, "OPEN", mode, source_mix, book_mix, oldest, "PENDING", system_type, reason),
    )
    card = db.fetchone("SELECT * FROM live_yankee_cards WHERE card_key=?", (card_key,))
    if not card:
        return None
    cid = int(card["id"])
    existing = int((db.fetchone("SELECT COUNT(*) AS n FROM live_yankee_legs WHERE card_id=?", (cid,)) or {}).get("n") or 0)
    if existing:
        return cid
    for order, (c, q) in enumerate(zip(candidates, quotes), 1):
        db.execute(
            """INSERT INTO live_yankee_legs(
              card_id,leg_order,candidate_id,source_lane,event_id,sport_key,league,market_key,selection,point,
              entry_odds,bookmaker_key,bookmaker_title,quote_captured_at,quote_age_minutes,min_odds,
              commence_time,home_team,away_team
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, order, int(c["id"]), c["source_lane"], c["event_id"], c.get("sport_key"), c.get("league"),
             c["market_key"], c["selection"], c.get("point"), float(q["price"]), q["bookmaker_key"],
             q.get("bookmaker_title"), q["captured_at"], float(q.get("quote_age_minutes") or 0),
             float(c.get("min_odds") or 0), c.get("commence_time"), c.get("home_team"), c.get("away_team")),
        )
        db.execute(
            "UPDATE live_yankee_candidates SET status='CONSUMED',consumed_card_id=? WHERE id=? AND status='QUEUED'",
            (cid, int(c["id"])),
        )
    return cid


def _expiry_rescue(db, now):
    ready = _ready(db, now)
    if len(ready) < 2:
        return {"formed": 0}
    earliest = min(x[2] for x in ready)
    minutes = (earliest - now).total_seconds() / 60.0
    if minutes > FALLBACK_TRIGGER_MINUTES:
        return {"formed": 0, "minutes_to_earliest": minutes}

    n = 4 if len(ready) >= 4 else 3 if len(ready) == 3 else 2
    system_type = "YANKEE" if n == 4 else "TRIXIE" if n == 3 else "DOUBLE"
    chosen = ready[:n]
    candidates = [x[0] for x in chosen]
    quote_maps = [x[1] for x in chosen]
    quotes, mode = _choose_quotes(db, candidates, quote_maps, now)
    cid = _create_card(db, candidates, quotes, system_type, mode, "EXPIRY_RESCUE")
    return {
        "formed": 1 if cid else 0,
        "card_id": cid,
        "system_type": system_type,
        "assembly_mode": mode,
        "minutes_to_earliest": minutes,
    }


def _expire(db, now):
    _ensure_schema(db)
    rescue = _expiry_rescue(db, now)
    if rescue.get("formed"):
        try:
            db.record_collector_run("LIVE_YANKEE_EXPIRY_RESCUE", True, detail=json.dumps(rescue, sort_keys=True))
        except Exception:
            pass
        print("LIVE_YANKEE_EXPIRY_RESCUE " + json.dumps(rescue, sort_keys=True), flush=True)
    return _ORIGINAL_EXPIRE(db, now)


def _form(db, now, max_cards: int = 5):
    out = dict(_ORIGINAL_FORM(db, now, max_cards=max_cards))
    try:
        db.execute("UPDATE live_yankee_cards SET system_type='YANKEE' WHERE system_type IS NULL OR system_type='' ")
    except Exception:
        pass
    return out


def _message(card: Mapping[str, Any], legs: Sequence[Mapping[str, Any]]) -> str:
    system_type = str(card.get("system_type") or ("YANKEE" if len(legs) == 4 else "TRIXIE" if len(legs) == 3 else "DOUBLE"))
    if system_type == "YANKEE" and str(card.get("formation_reason") or "") != "EXPIRY_RESCUE":
        return _ORIGINAL_MESSAGE(card, legs)

    lines_count = SYSTEM_LINES.get(system_type, max(1, len(legs)))
    mode = str(card.get("assembly_mode") or "")
    mode_label = (
        "SAME-BOOK VERIFIED" if mode == "SAME_BOOK_VERIFIED"
        else "SAME-BOOK CONSOLIDATED" if mode == "SAME_BOOK_CONSOLIDATED"
        else "CROSS-BOOK SYNTHETIC"
    )
    icon = "🟢" if mode == "SAME_BOOK_VERIFIED" else "🟡" if mode == "SAME_BOOK_CONSOLIDATED" else "🟠"
    title = f"{icon} LIVE MULTIPLES — {system_type} EXPIRY RESCUE · {mode_label}"
    lines = [
        title, "",
        f"🎯 {system_type.title()} #{card.get('id')} · {len(legs)} selections · {lines_count} line{'s' if lines_count != 1 else ''}",
        f"💷 Reference stake: £{REFERENCE_TOTAL_STAKE_GBP:.2f} total (~£{REFERENCE_TOTAL_STAKE_GBP/lines_count:.2f}/line)",
        f"Sources: {card.get('source_mix') or '—'}", "",
    ]
    for i, leg in enumerate(legs, 1):
        fixture = f"{leg.get('home_team') or ''} v {leg.get('away_team') or ''}".strip()
        kickoff = feed._parse_iso(leg.get("commence_time"))
        kt = kickoff.strftime("%d %b %H:%M UTC") if kickoff else str(leg.get("commence_time") or "—")
        lines += [
            f"{i}. [{leg.get('source_lane')}] {feed._market_label(leg)} @ {float(leg.get('entry_odds') or 0):.2f}",
            f"   {leg.get('bookmaker_title') or leg.get('bookmaker_key')} · {fixture} · {kt}",
        ]
    lines += ["", f"Oldest quote: {float(card.get('oldest_quote_age_minutes') or 0):.1f} min"]
    if mode in {"SAME_BOOK_VERIFIED", "SAME_BOOK_CONSOLIDATED"}:
        lines += [
            f"Book: {legs[0].get('bookmaker_title') or legs[0].get('bookmaker_key')}",
            "Expiry rescue: these legs were used before the earliest selection became unplaceable.",
            "VERIFY ALL PRICES/MARKETS BEFORE PLACING. Manual/shadow feed only.",
        ]
    else:
        lines += [
            f"Books: {', '.join(sorted({str(x.get('bookmaker_title') or x.get('bookmaker_key')) for x in legs}))}",
            "CROSS-BOOK SYNTHETIC: no single supported bookmaker met the consolidation guardrail.",
            "This records the research combination but is not one verified placeable system bet.",
        ]
    return "\n".join(lines)


def _send(db, settings, limit: int = 5):
    if not bool(getattr(settings, "telegram_alerts_enabled", False)):
        return {"enabled": False, "sent": 0, "reason": "disabled"}
    token = str(getattr(settings, "telegram_bot_token", "") or "").strip()
    chat = str(getattr(settings, "telegram_chat_id", "") or "").strip()
    if not token or not chat:
        return {"enabled": True, "sent": 0, "reason": "credentials_missing"}
    cards = db.fetchall("SELECT * FROM live_yankee_cards WHERE telegram_status!='SENT' ORDER BY id LIMIT ?", (limit,))
    sent = failed = eligible = 0
    for card in cards:
        legs = db.fetchall("SELECT * FROM live_yankee_legs WHERE card_id=? ORDER BY leg_order", (int(card["id"]),))
        if len(legs) not in {2, 3, 4}:
            continue
        eligible += 1
        try:
            _send_telegram(token, chat, feed._message(card, legs))
            db.execute(
                "UPDATE live_yankee_cards SET telegram_status='SENT',telegram_attempts=telegram_attempts+1,telegram_sent_at=?,telegram_error=NULL WHERE id=?",
                (utc_now_iso(), int(card["id"])),
            )
            sent += 1
        except Exception as exc:
            db.execute(
                "UPDATE live_yankee_cards SET telegram_status='FAILED',telegram_attempts=telegram_attempts+1,telegram_error=? WHERE id=?",
                (sanitize_sensitive_text(exc), int(card["id"])),
            )
            failed += 1
    return {"enabled": True, "eligible": eligible, "sent": sent, "failed": failed}


def _scoreboard(db):
    _ensure_schema(db)
    out = dict(_ORIGINAL_SCOREBOARD(db))
    rows = db.fetchall("SELECT system_type,COUNT(*) AS n FROM live_yankee_cards GROUP BY system_type")
    counts = {str(r.get("system_type") or "YANKEE"): int(r.get("n") or 0) for r in rows}
    out["systems"] = {
        "yankees": counts.get("YANKEE", 0),
        "trixies": counts.get("TRIXIE", 0),
        "doubles": counts.get("DOUBLE", 0),
    }
    out["expiry_fallback"] = {
        "trigger_minutes": FALLBACK_TRIGGER_MINUTES,
        "three_ready": "TRIXIE",
        "two_ready": "DOUBLE",
        "one_ready": "EXPIRE_UNUSED",
        "consume_once": True,
    }
    return out


def _page_html():
    html = _ORIGINAL_PAGE()
    try:
        counts = _scoreboard(web_module.db).get("systems", {})
    except Exception:
        counts = {}
    panel = (
        "<div class='panel'><h2>Expiry fallback</h2>"
        "<div class='muted'>Yankee remains first priority. At 10 minutes before the earliest price-ready kickoff, "
        "3 unique ready events become a Trixie and 2 become a Double. Used legs are consumed once and cannot be recycled. "
        "Same-book verified/consolidated pricing is preferred; cross-book combinations remain explicitly synthetic.</div>"
        "<div class='grid compact' style='margin-top:12px'>"
        f"<div class='card'><div class='label'>YANKEES</div><div class='value'>{int(counts.get('yankees',0))}</div></div>"
        f"<div class='card'><div class='label'>TRIXIES</div><div class='value'>{int(counts.get('trixies',0))}</div></div>"
        f"<div class='card'><div class='label'>DOUBLES</div><div class='value'>{int(counts.get('doubles',0))}</div></div>"
        "</div></div>"
    )
    return html.replace("</body>", panel + "</body>", 1)


def install() -> dict:
    global _INSTALLED, _ORIGINAL_EXPIRE, _ORIGINAL_FORM, _ORIGINAL_MESSAGE, _ORIGINAL_SCOREBOARD, _ORIGINAL_PAGE, _ORIGINAL_TARGETS
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _ensure_schema(web_module.db)
    _ORIGINAL_EXPIRE = feed._expire
    _ORIGINAL_FORM = feed._form
    _ORIGINAL_MESSAGE = feed._message
    _ORIGINAL_SCOREBOARD = feed.scoreboard
    _ORIGINAL_PAGE = feed._page_html
    _ORIGINAL_TARGETS = feed._targets

    feed._expire = _expire
    feed._form = _form
    feed._message = _message
    feed._send = _send
    feed.scoreboard = _scoreboard
    feed._page_html = _page_html
    feed._targets = _targets
    feed.ALGORITHM_VERSION = VERSION

    try:
        state = feed._state(web_module.db)
        policy = json.loads(str(state.get("source_policy_json") or "{}"))
        policy["expiry_fallback"] = {
            "trigger_minutes": FALLBACK_TRIGGER_MINUTES,
            "3_ready": "TRIXIE",
            "2_ready": "DOUBLE",
            "1_ready": "EXPIRE_UNUSED",
            "consume_once": True,
        }
        web_module.db.execute(
            "UPDATE live_yankee_state SET algorithm_version=?,source_policy_json=? WHERE singleton_id=1",
            (VERSION, json.dumps(policy, sort_keys=True)),
        )
    except Exception:
        pass

    web_module.VERSION = "0.19.47"
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "trigger_minutes": FALLBACK_TRIGGER_MINUTES,
        "systems": ["YANKEE", "TRIXIE", "DOUBLE"],
        "dashboard_version": web_module.VERSION,
    }
