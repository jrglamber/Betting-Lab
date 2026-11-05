from __future__ import annotations

"""Sole Betting Lab Telegram producer: prospective, manually placeable HR2 cards.

Covers HR2 Double, Trixie, Yankee, Heinz using the ORIGINAL frozen card/leg
ledger, not separately assembled shadow portfolio or quality challenger.
All previous cards are seeded PRE_WIRE on first activation; never replay them.
No provider calls, bet placement, card edits or new scoring/selection rules.
"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import prod
from typing import Any
from zoneinfo import ZoneInfo

import hit_rate_multiples_lab as hr
import hit_rate_same_book_only_patch as same_book
import hr_market_integrity as integrity
import hr_portfolio_telegram as portfolio_tg
import telegram_alerts
from db import sanitize_sensitive_text, utc_now_iso

VERSION = "HR2_BET_READY_TELEGRAM_ONLY_V1"
SOURCE = "HR2_BET_READY"
POLICY_VERSION = "HRM_2026-10-18_V3_THEORETICAL_QUEUE"
BOOKS = {"williamhill": "William Hill", "ladbrokes_uk": "Ladbrokes"}
FORMAT_LINES = {"DOUBLE": (2, 1), "TRIXIE": (3, 4), "YANKEE": (4, 11), "HEINZ": (6, 57)}
QUOTE_MAX_AGE_MINUTES = 15.0
MIN_LEAD_MINUTES = 10.0
MAX_ITEMS_PER_CYCLE = 30
_INSTALLED = False
_PREVIOUS_FORM = None


def _schema(db):
    pk = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute("""CREATE TABLE IF NOT EXISTS hr2_telegram_control (
        id INTEGER PRIMARY KEY, activated_at TEXT NOT NULL,
        version TEXT NOT NULL, allowed_source TEXT NOT NULL,
        last_check_at TEXT, last_error TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hr2_telegram_alerts (
        id {pk}, card_id INTEGER NOT NULL UNIQUE, card_key TEXT NOT NULL,
        format TEXT NOT NULL, signature TEXT,
        status TEXT NOT NULL, created_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, sent_at TEXT,
        telegram_message_id TEXT, error TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_hr2_tg_status ON hr2_telegram_alerts(status,created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_hr2_tg_sig ON hr2_telegram_alerts(signature,status)")


def _state(db):
    row = db.fetchone("SELECT * FROM hr2_telegram_control WHERE id=1")
    if row:
        return dict(row)
    # Activate first, then seed every card which already exists. On restart
    # the durable control row prevents marking newly formed post-activation
    # cards PRE_WIRE or replaying old cards after a deployment.
    stamp = utc_now_iso()
    db.execute("""INSERT INTO hr2_telegram_control(
        id,activated_at,version,allowed_source
    ) VALUES(1,?,?,?) ON CONFLICT(id) DO NOTHING""", (stamp, VERSION, SOURCE))
    for card in db.fetchall(
        "SELECT id,card_key,system_type,created_at FROM hit_rate_cards "
        "WHERE experiment_key='HR2' ORDER BY id"
    ):
        db.execute("""INSERT INTO hr2_telegram_alerts(
            card_id,card_key,format,status,created_at
        ) VALUES(?,?,?,?,?) ON CONFLICT(card_id) DO NOTHING""", (
            int(card["id"]), str(card.get("card_key") or ""),
            str(card.get("system_type") or ""), "PRE_WIRE",
            str(card.get("created_at") or stamp),
        ))
    return dict(db.fetchone("SELECT * FROM hr2_telegram_control WHERE id=1"))


def _silent(*_args, **_kwargs):
    return {"enabled": False, "sent": 0, "reason": "hr2_bet_ready_only"}


def _parse(value):
    return hr._parse(value)


def _signature(card, legs):
    bits = sorted(repr(integrity.selection_key(x)) for x in legs)
    return sha256((str(card["system_type"]) + "|" + "|".join(bits)).encode()).hexdigest()


def _valid(card, legs, now):
    fmt = str(card.get("system_type") or "").upper()
    if str(card.get("experiment_key")) != "HR2" or str(card.get("policy_version")) != POLICY_VERSION:
        return False, "wrong_experiment"
    if fmt not in FORMAT_LINES or len(legs) != FORMAT_LINES[fmt][0]:
        return False, "wrong_leg_count"
    if str(card.get("status") or "") != "OPEN":
        return False, "not_open"
    if str(card.get("assembly_mode") or "") not in {"SAME_BOOK_VERIFIED", "SAME_BOOK_CONSOLIDATED"}:
        return False, "not_placeable_same_book"
    book_set = {str(x.get("bookmaker_key") or "") for x in legs}
    if len(book_set) != 1 or next(iter(book_set)) not in BOOKS:
        return False, "bookmaker_not_supported"
    if len({integrity.fixture_key(x) for x in legs}) != len(legs):
        return False, "duplicate_fixture"
    if any(not integrity.market_actionable(x) for x in legs):
        return False, "market_line_missing"
    for leg in legs:
        try:
            odds = float(leg.get("entry_odds") or 0)
        except (TypeError, ValueError):
            return False, "invalid_quote_price"
        if odds <= 1:
            return False, "invalid_quote_price"
        quote = _parse(leg.get("quote_captured_at"))
        kickoff = _parse(leg.get("commence_time"))
        if quote is None or quote > now or (now - quote).total_seconds() > QUOTE_MAX_AGE_MINUTES * 60:
            return False, "stale_quote"
        if kickoff is None or kickoff <= now + timedelta(minutes=MIN_LEAD_MINUTES):
            return False, "kickoff_too_close"
    return True, "ok"


def _message(card, legs):
    fmt = str(card["system_type"]).upper()
    _, lines = FORMAT_LINES[fmt]
    stake = float(card.get("reference_stake_gbp") or hr.REFERENCE_STAKE_GBP)
    book = BOOKS[legs[0]["bookmaker_key"]]
    uk = ZoneInfo("Europe/London")
    message = [
        f"🟢 HR2 {fmt} — BET READY",
        "",
        f"Bookmaker: {book} (ALL selections at this bookmaker)",
        f"Card: #{card['id']} · {card.get('assembly_mode')}",
        f"Reference stake: £{stake:.2f} TOTAL · {lines} line(s) (~£{stake/lines:.2f}/line)",
        "",
    ]
    for i, leg in enumerate(legs, 1):
        kick = _parse(leg.get("commence_time"))
        when = kick.astimezone(uk).strftime("%d %b %H:%M UK") if kick else "Time unknown"
        fixture = f"{leg.get('home_team') or '?'} v {leg.get('away_team') or '?'}"
        message.append(
            f"{i}. {integrity.market_label(leg)} @ {float(leg['entry_odds']):.2f}"
        )
        message.append(f"   {fixture} · {when}")
    message.extend([
        "",
        "MANUAL bet only. Prices were observed earlier and can move.",
        "Check every selection, market line and price at the named bookmaker before placing.",
        "Research/shadow card — NO bet has been placed automatically.",
    ])
    return "\n".join(message)


def _record(db, card, status, *, signature=None, error=None):
    db.execute("""INSERT INTO hr2_telegram_alerts(
        card_id,card_key,format,signature,status,created_at,error
    ) VALUES(?,?,?,?,?,?,?) ON CONFLICT(card_id) DO NOTHING""", (
        int(card["id"]), str(card.get("card_key") or ""),
        str(card.get("system_type") or ""), signature,
        status, str(card.get("created_at") or utc_now_iso()), error,
    ))
    if status != "PENDING":
        db.execute("""UPDATE hr2_telegram_alerts SET
            status=?, signature=?, error=? WHERE card_id=? AND status NOT IN ('SENT','SENDING','UNCERTAIN','PRE_WIRE')""",
            (status, signature, error, int(card["id"])))
    return dict(db.fetchone(
        "SELECT * FROM hr2_telegram_alerts WHERE card_id=?", (int(card["id"]),)
    ) or {})


def send_due(db, settings, *, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    _schema(db)
    state = _state(db)
    if not bool(getattr(settings, "telegram_alerts_enabled", False)):
        return {"enabled": False, "sent": 0, "reason": "telegram_disabled"}
    token = str(getattr(settings, "telegram_bot_token", "") or "").strip()
    chat = str(getattr(settings, "telegram_chat_id", "") or "").strip()
    if not token or not chat:
        return {"enabled": False, "sent": 0, "reason": "credentials_missing"}

    cards = db.fetchall("""SELECT c.* FROM hit_rate_cards c
        LEFT JOIN hr2_telegram_alerts a ON a.card_id=c.id
        WHERE c.experiment_key='HR2' AND c.policy_version=?
          AND c.created_at>=? AND a.card_id IS NULL
        ORDER BY c.created_at,c.id LIMIT ?""",
        (POLICY_VERSION, state["activated_at"], MAX_ITEMS_PER_CYCLE),
    )
    valid_cards = []
    rejected = 0
    for raw in cards:
        card = dict(raw)
        legs = [dict(x) for x in db.fetchall(
            "SELECT * FROM hit_rate_legs WHERE card_id=? ORDER BY leg_order",
            (int(card["id"]),),
        )]
        valid, why = _valid(card, legs, now)
        signature = _signature(card, legs) if legs else None
        if not valid:
            _record(db, card, "SKIPPED_UNPLACEABLE", signature=signature, error=why)
            rejected += 1
            continue
        valid_cards.append((card, legs, signature))

    # Two otherwise identical system cards created at separate bookmakers
    # yield just one alert: the best frozen combined price wins.
    best_by_signature = {}
    for card, legs, sig in valid_cards:
        score = prod(float(x["entry_odds"]) for x in legs)
        previous = best_by_signature.get(sig)
        if previous is None or score > previous[0]:
            best_by_signature[sig] = (score, card, legs)
    eligible = []
    duplicate = 0
    for card, legs, sig in valid_cards:
        best = best_by_signature[sig][1]
        if int(best["id"]) != int(card["id"]):
            _record(db, card, "SUPPRESSED_DUPLICATE", signature=sig)
            duplicate += 1
            continue
        prior = db.fetchone("""SELECT card_id FROM hr2_telegram_alerts
            WHERE signature=? AND status IN ('SENT','SENDING','UNCERTAIN')
            AND card_id<>? LIMIT 1""", (sig, int(card["id"])))
        if prior:
            _record(db, card, "SUPPRESSED_DUPLICATE", signature=sig)
            duplicate += 1
            continue
        eligible.append((card, legs, sig))

    sent = uncertain = 0
    for card, legs, sig in eligible:
        row = _record(db, card, "PENDING", signature=sig)
        if row.get("status") != "PENDING":
            continue
        # Never automatically replay ambiguous network attempts. If Telegram
        # accepted a message but the HTTP response was lost, a blind retry
        # would create duplicate bet prompts.
        claimed = db.execute("""UPDATE hr2_telegram_alerts SET
            status='SENDING', attempts=attempts+1
            WHERE card_id=? AND status='PENDING'""", (int(card["id"]),))
        if not claimed:
            continue
        try:
            result = telegram_alerts._send_telegram(
                token, chat, _message(card, legs), source=SOURCE,
            )
            if not result.get("ok"):
                raise RuntimeError("Telegram delivery not confirmed")
            db.execute("""UPDATE hr2_telegram_alerts SET
                status='SENT',sent_at=?,telegram_message_id=?,error=NULL
                WHERE card_id=? AND status='SENDING'""", (
                utc_now_iso(), str(result.get("message_id") or ""),
                int(card["id"]),
            ))
            sent += 1
        except Exception as exc:
            db.execute("""UPDATE hr2_telegram_alerts SET status='UNCERTAIN',
                error=? WHERE card_id=? AND status='SENDING'""", (
                sanitize_sensitive_text(exc), int(card["id"]),
            ))
            uncertain += 1
    db.execute(
        "UPDATE hr2_telegram_control SET last_check_at=?,last_error=NULL WHERE id=1",
        (utc_now_iso(),),
    )
    return {"enabled": True, "source": SOURCE,
            "eligible": len(eligible), "sent": sent, "uncertain": uncertain,
            "rejected": rejected, "duplicates_suppressed": duplicate,
            "checked_cards": len(cards)}


def status(db, settings):
    _schema(db)
    state = _state(db)
    counts = db.fetchall("""SELECT status,COUNT(*) AS n
        FROM hr2_telegram_alerts GROUP BY status ORDER BY status""")
    recent = db.fetchall("""SELECT card_id,format,status,created_at,sent_at,error
        FROM hr2_telegram_alerts ORDER BY id DESC LIMIT 20""")
    return {
        "version": VERSION, "only_notification_source": SOURCE,
        "allowed_experiment": "HR2", "allowed_formats": list(FORMAT_LINES),
        "manual_books": list(BOOKS.values()),
        "enabled": bool(getattr(settings, "telegram_alerts_enabled", False)),
        "credentials_configured": bool(
            getattr(settings, "telegram_bot_token", None) and
            getattr(settings, "telegram_chat_id", None)
        ),
        "activated_at": state["activated_at"],
        "last_check_at": state.get("last_check_at"),
        "counts": [dict(x) for x in counts],
        "recent": [dict(x) for x in recent],
        "portfolio_shadow_muted": not portfolio_tg.ROUTING_ENABLED,
        "no_auto_bets": True, "no_extra_api_calls": True,
    }


def install(app, db, settings):
    global _INSTALLED, _PREVIOUS_FORM
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}

    _schema(db)
    state = _state(db)
    # Preserve the portfolio research and status API; disable only its outbound
    # alerts. Its existing worker wrapper calls the global send_due dynamically.
    portfolio_tg.ROUTING_ENABLED = False
    # Existing legacy and test senders are silent without affecting collectors.
    portfolio_tg._disable_other_senders()
    same_book._send_pending = _silent

    # Intercept actual HR card formation (which is also reached when callers
    # captured an older maintenance function). Never send a test message.
    _PREVIOUS_FORM = hr._form

    def with_hr2_alerts(db_obj, now):
        formed = _PREVIOUS_FORM(db_obj, now)
        try:
            info = send_due(db_obj, settings, now=now)
            if info.get("sent") or info.get("uncertain") or info.get("rejected"):
                print(f"HR2_TELEGRAM_MAINT {info}", flush=True)
        except Exception as exc:
            error = sanitize_sensitive_text(exc)
            print(f"HR2_TELEGRAM_ERROR {type(exc).__name__}: {error}", flush=True)
            db_obj.execute(
                "UPDATE hr2_telegram_control SET last_check_at=?,last_error=? WHERE id=1",
                (utc_now_iso(), error),
            )
        return formed

    hr._form = with_hr2_alerts
    @app.get("/api/hr2-telegram/status")
    def hr2_telegram_status():
        return status(db, settings)

    tables = ("hr2_telegram_control", "hr2_telegram_alerts")
    try:
        import exporter
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + tables))
    except Exception:
        pass
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + tables))
        split_exporter.MANUAL_TABLES = tuple(dict.fromkeys(tuple(split_exporter.MANUAL_TABLES) + tables))
    except Exception:
        pass
    _INSTALLED = True
    return {
        "installed": True, "version": VERSION,
        "only_notification_source": SOURCE,
        "allowed_experiment": "HR2",
        "allowed_formats": list(FORMAT_LINES),
        "manual_books": list(BOOKS.values()),
        "preexisting_cards_suppressed": True,
        "activated_at": state["activated_at"],
        "telegram_configured": bool(
            getattr(settings, "telegram_alerts_enabled", False) and
            getattr(settings, "telegram_bot_token", None) and
            getattr(settings, "telegram_chat_id", None)),
        "other_senders_muted": True,
        "provider_calls_added": 0,
        "betting_authority": False,
    }
