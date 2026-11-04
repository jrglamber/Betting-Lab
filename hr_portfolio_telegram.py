from __future__ import annotations

"""HR Portfolio Shadow is the sole Telegram notification source.

Fires only on newly formed, still-manually-placeable chronological portfolio
groups, not quality challenger comparisons or live betting/execution.
Research, odds collection, settlements and old cards continue independently.
The Telegram transport separately enforces an outbound allowlist.
"""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import prod
from typing import Any
import json

from db import sanitize_sensitive_text, utc_now_iso
import hr_portfolio_shadow as portfolio
import hr_market_integrity as integrity
import hit_rate_multiples_lab as hr
import telegram_alerts

VERSION = "HR_PORTFOLIO_TELEGRAM_ONLY_V1"
ALLOWED_SOURCE = "HR_PORTFOLIO_SHADOW"
BOOK_NAMES = {"williamhill": "William Hill", "ladbrokes_uk": "Ladbrokes"}
MAX_QUOTE_AGE_MINUTES = 15.0
MAX_QUOTE_SPREAD_MINUTES = 5.0
MAX_DAILY_ALERTS = 8
MAX_SEND_ATTEMPTS = 2
_INSTALLED = False


def _stamp(value):
    return hr._parse(value)


def _schema(db):
    ident = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute("""CREATE TABLE IF NOT EXISTS hr_portfolio_telegram_state (
        id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
        version TEXT NOT NULL, allowed_source TEXT NOT NULL,
        last_check_at TEXT, last_check_error TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hr_portfolio_telegram_alerts (
        id {ident}, group_id INTEGER NOT NULL UNIQUE,
        group_key TEXT NOT NULL, selection_signature TEXT NOT NULL,
        bookmaker_key TEXT NOT NULL, created_at TEXT NOT NULL,
        status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        last_attempt_at TEXT, sent_at TEXT, telegram_message_id TEXT,
        last_error TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_hrpt_alert_status ON hr_portfolio_telegram_alerts(status,created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_hrpt_alert_signature ON hr_portfolio_telegram_alerts(selection_signature,status)")


def _state(db):
    row = db.fetchone("SELECT * FROM hr_portfolio_telegram_state WHERE id=1")
    if row:
        return dict(row)
    db.execute("""INSERT INTO hr_portfolio_telegram_state(
        id,started_at,version,allowed_source
    ) VALUES(1,?,?,?) ON CONFLICT(id) DO NOTHING""",
               (utc_now_iso(), VERSION, ALLOWED_SOURCE))
    return dict(db.fetchone("SELECT * FROM hr_portfolio_telegram_state WHERE id=1"))


def _silent_legacy(*_args, **_kwargs):
    return {
        "enabled": False, "sent": 0,
        "reason": "hr_portfolio_shadow_only",
        "allowed_source": ALLOWED_SOURCE,
    }


def _disable_other_senders():
    """Stop legacy jobs from attempting sends or producing noisy failures.

    The independent Telegram transport allowlist is a second safeguard.
    """
    import live_yankee_feed
    import manual_systems_shadow
    import web
    telegram_alerts.send_pending_heinz_alerts = _silent_legacy
    telegram_alerts.send_telegram_test_now = _silent_legacy
    manual_systems_shadow.send_pending_heinz_alerts = _silent_legacy
    web.send_pending_heinz_alerts = _silent_legacy
    web.send_telegram_test_now = _silent_legacy
    live_yankee_feed._send = _silent_legacy


def _valid(group, legs, now):
    count = int(group.get("selection_count") or 0)
    if group.get("policy_version") != portfolio.VERSION or group.get("cohort") != "UNIFIED":
        return False, "wrong_cohort"
    if str(group.get("status")) != "OPEN":
        return False, "not_open"
    if str(group.get("bookmaker_key")) not in portfolio.MANUAL_BOOKS:
        return False, "not_supported_bookmaker"
    if count not in (2, 3, 4) or len(legs) != count:
        return False, "incomplete_group"
    if len({integrity.fixture_key(x) for x in legs}) != count:
        return False, "repeat_fixture"
    if any(not integrity.market_actionable(x) for x in legs):
        return False, "market_line_missing"
    quotes = [_stamp(x.get("quote_captured_at")) for x in legs]
    kicks = [_stamp(x.get("commence_time")) for x in legs]
    if not all(quotes) or not all(kicks):
        return False, "missing_time"
    if any(str(x.get("bookmaker_key")) != str(group.get("bookmaker_key")) for x in legs):
        return False, "mixed_book"
    if any(float(x.get("entry_odds") or 0) <= 1.0 for x in legs):
        return False, "invalid_price"
    if any(t > now or (now-t) > timedelta(minutes=MAX_QUOTE_AGE_MINUTES) for t in quotes):
        return False, "stale_or_future_quote"
    if max(quotes)-min(quotes) > timedelta(minutes=MAX_QUOTE_SPREAD_MINUTES):
        return False, "noncoherent_quotes"
    if any(kick <= now+timedelta(minutes=portfolio.MIN_LEAD_MINUTES) for kick in kicks):
        return False, "kickoff_too_close"
    return True, "ok"


def _signature(legs):
    identities = sorted(
        [list(integrity.selection_key(x)) for x in legs],
        key=lambda item: json.dumps(item, sort_keys=True),
    )
    return sha256(json.dumps(identities, sort_keys=True).encode("utf-8")).hexdigest()


def _market_label(leg):
    market = str(leg.get("market_key") or "").lower()
    selection = str(leg.get("selection") or "—")
    point = leg.get("point")
    if market == "h2h":
        return f"Match winner: {selection}"
    if market in ("btts", "both_teams_to_score"):
        return f"BTTS: {selection}"
    if market in ("totals", "total", "over_under", "alternate_totals"):
        return f"Total goals/points: {selection}" + (f" {point:g}" if isinstance(point, (int,float)) else f" {point}" if point is not None else "")
    return f"{market}: {selection}" + (f" ({point})" if point is not None else "")


def _time_label(value):
    dt = _stamp(value)
    return dt.strftime("%a %d %b %H:%M UTC") if dt else "—"


def _message(group, legs, formats, now):
    count = len(legs)
    book = BOOK_NAMES[str(group["bookmaker_key"])]
    reference = float(group.get("reference_stake_gbp") or portfolio.TOTAL_STAKE_GBP)
    ordered = sorted(legs, key=lambda l: int(l["leg_order"]))
    quote_age = max((now-_stamp(x["quote_captured_at"])).total_seconds()/60 for x in ordered)
    soonest = min(_stamp(x["commence_time"]) for x in ordered)
    lines = [
        "🧪 HR PORTFOLIO SHADOW — NEW MANUAL RESEARCH CARD",
        "",
        f"Group #{group['id']} · {count} selections · {book} ONLY",
        f"Reference stake: £{reference:.2f} TOTAL across formats (not £10 each)",
        "Source mix: " + ", ".join(sorted({str(x.get("source_lane") or "UNKNOWN") for x in ordered})),
        "",
    ]
    for i, leg in enumerate(ordered, 1):
        fixture = f"{leg.get('home_team') or '?'} v {leg.get('away_team') or '?'}"
        lines += [
            f"{i}. {fixture}",
            f"   {_market_label(leg)} @ {float(leg['entry_odds']):.2f}",
            f"   {leg.get('source_lane') or '?'} · {_time_label(leg.get('commence_time'))}",
        ]
    lines.extend(["", "🔢 Fixed shadow format split:"])
    names = {"DOUBLE": ("Double", 1, 2, 1),
             "TRIXIE": ("Trixie", 1, 3, 4),
             "YANKEE": ("Yankee", 1, 4, 11)}
    for fmt in formats:
        share = float(fmt.get("allocation_pct") or 0)
        if share <= 0 or str(fmt.get("format")) not in names:
            continue
        label, start, end, nlines = names[fmt["format"]]
        stake = reference*share
        lines.append(
            f"• {label} (legs {start}–{end}): £{stake:.2f} total "
            f"({nlines} lines, ~£{stake/nlines:.2f}/line)"
        )
    lines += [
        "",
        f"Latest quote age: {quote_age:.1f} min",
        f"First kickoff: {soonest.strftime('%a %d %b %H:%M UTC')}",
        "",
        "⚠️ RECHECK bookmaker, markets, prices and kickoff before manual placement.",
        "This is a SHADOW research alert, NOT a validated live recommendation.",
        "The bot does not place bets or guarantee quoted odds.",
        "https://betting-lab-production.up.railway.app/hr-portfolio-shadow",
    ]
    return "\n".join(lines)


def _record(db, group, signature, status, error=None):
    existing = db.fetchone(
        "SELECT * FROM hr_portfolio_telegram_alerts WHERE group_id=?",
        (int(group["id"]),),
    )
    if existing:
        return dict(existing)
    db.execute("""INSERT INTO hr_portfolio_telegram_alerts(
        group_id,group_key,selection_signature,bookmaker_key,created_at,status,last_error
    ) VALUES(?,?,?,?,?,?,?) ON CONFLICT(group_id) DO NOTHING""", (
        int(group["id"]), str(group["group_key"]), signature,
        str(group["bookmaker_key"]), utc_now_iso(), status, error,
    ))
    return dict(db.fetchone(
        "SELECT * FROM hr_portfolio_telegram_alerts WHERE group_id=?",
        (int(group["id"]),),
    ))


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
        return {"enabled": True, "sent": 0, "reason": "credentials_missing"}
    groups = db.fetchall("""SELECT * FROM hr_portfolio_groups
        WHERE created_at>=? AND policy_version=? AND cohort='UNIFIED'
        ORDER BY created_at,id LIMIT 100""",
        (state["started_at"], portfolio.VERSION))
    ready = []
    stale = 0
    for g in groups:
        gid = int(g["id"])
        legs = db.fetchall("SELECT * FROM hr_portfolio_legs WHERE group_id=? ORDER BY leg_order", (gid,))
        log = db.fetchone("SELECT * FROM hr_portfolio_telegram_alerts WHERE group_id=?", (gid,))
        if log and str(log["status"]) in ("SENT", "SUPPRESSED_DUPLICATE", "EXPIRED", "UNCERTAIN", "SENDING"):
            continue
        if log and int(log.get("attempts") or 0) >= MAX_SEND_ATTEMPTS:
            continue
        valid, reason = _valid(g, legs, now)
        sig = _signature(legs) if legs else sha256(str(gid).encode()).hexdigest()
        if not valid:
            _record(db, g, sig, "EXPIRED", error=reason)
            if log and log.get("status") == "FAILED":
                db.execute("UPDATE hr_portfolio_telegram_alerts SET status='EXPIRED',last_error=? WHERE group_id=?",
                           (reason, gid))
            stale += 1
            continue
        ready.append((dict(g), [dict(x) for x in legs], sig))
    # If the same set of selections is available at two permitted bookmakers,
    # send only one alert, preferring the better combination at one bookmaker.
    groups_by_sig = {}
    for g, legs, sig in ready:
        groups_by_sig.setdefault(sig, []).append((g, legs, sig))
    queued = []
    duplicate = 0
    for sig, choices in groups_by_sig.items():
        choices.sort(key=lambda x: (-prod(float(l["entry_odds"]) for l in x[1]), int(x[0]["id"])))
        queued.append(choices[0])
        for g, _, _ in choices[1:]:
            _record(db, g, sig, "SUPPRESSED_DUPLICATE", error="better_same_book_group")
            duplicate += 1
    sent = failed = 0
    for g, legs, sig in sorted(queued, key=lambda x: (x[0]["created_at"], x[0]["id"])):
        prior_sent = db.fetchone("""SELECT group_id FROM hr_portfolio_telegram_alerts
            WHERE selection_signature=? AND status IN ('SENT','SENDING','UNCERTAIN')
            AND group_id<>? LIMIT 1""", (sig, int(g["id"])))
        if prior_sent:
            _record(db, g, sig, "SUPPRESSED_DUPLICATE", error="already_alerted_same_legs")
            duplicate += 1
            continue
        daily = db.fetchone("""SELECT COUNT(*) AS n FROM hr_portfolio_telegram_alerts
            WHERE status='SENT' AND sent_at>=?""",
            (now.strftime("%Y-%m-%dT00:00:00+00:00"),))
        if int((daily or {}).get("n") or 0) >= MAX_DAILY_ALERTS:
            break
        record = _record(db, g, sig, "PENDING")
        if str(record["status"]) not in ("PENDING", "FAILED"):
            continue
        db.execute("""UPDATE hr_portfolio_telegram_alerts
            SET status='SENDING',attempts=attempts+1,last_attempt_at=?,
                last_error=NULL WHERE group_id=? AND status IN ('PENDING','FAILED')""",
            (utc_now_iso(), int(g["id"])))
        formats = db.fetchall(
            "SELECT * FROM hr_portfolio_formats WHERE group_id=? ORDER BY format",
            (int(g["id"]),),
        )
        try:
            result = telegram_alerts._send_telegram(
                token, chat, _message(g, legs, formats, now),
                source=ALLOWED_SOURCE,
            )
            if not result.get("ok"):
                raise RuntimeError("Telegram did not confirm delivery")
            db.execute("""UPDATE hr_portfolio_telegram_alerts
                SET status='SENT',sent_at=?,telegram_message_id=?,last_error=NULL
                WHERE group_id=?""",
                (utc_now_iso(), str(result.get("message_id") or ""), int(g["id"])),
            )
            sent += 1
        except Exception as exc:
            db.execute("""UPDATE hr_portfolio_telegram_alerts SET status='FAILED',
                last_error=? WHERE group_id=?""",
                (sanitize_sensitive_text(exc), int(g["id"])),
            )
            failed += 1
    db.execute(
        "UPDATE hr_portfolio_telegram_state SET last_check_at=?,last_check_error=NULL WHERE id=1",
        (utc_now_iso(),),
    )
    return {"enabled": True, "sent": sent, "failed": failed,
            "duplicates_suppressed": duplicate, "expired": stale,
            "candidate_groups": len(groups), "ready_groups": len(queued),
            "allowed_source": ALLOWED_SOURCE}


def status(db, settings):
    _schema(db)
    state = _state(db)
    counters = db.fetchall("""SELECT status,COUNT(*) AS count
        FROM hr_portfolio_telegram_alerts GROUP BY status ORDER BY status""")
    recent = db.fetchall("""SELECT group_id,bookmaker_key,status,attempts,
        created_at,sent_at,last_error
        FROM hr_portfolio_telegram_alerts ORDER BY id DESC LIMIT 20""")
    return {
        "version": VERSION, "only_notification_source": ALLOWED_SOURCE,
        "enabled": bool(getattr(settings, "telegram_alerts_enabled", False)),
        "credentials_configured": bool(
            getattr(settings, "telegram_bot_token", None) and
            getattr(settings, "telegram_chat_id", None)),
        "started_at": state["started_at"],
        "last_check_at": state.get("last_check_at"),
        "last_check_error": state.get("last_check_error"),
        "max_daily_alerts": MAX_DAILY_ALERTS,
        "counts": [dict(x) for x in counters],
        "recent": [dict(x) for x in recent],
        "no_live_bet_authority": True,
        "legacy_senders_suppressed": True,
    }


def install(app, db, settings):
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    if not portfolio._INSTALLED:
        raise RuntimeError("HR Portfolio Shadow must be installed before Telegram routing")
    _schema(db)
    state = _state(db)
    # Disable old senders before workers start; code-level transport allowlist
    # additionally rejects any bypassed sender.
    _disable_other_senders()
    previous = portfolio.run_maintenance

    def with_alerts(*args, **kwargs):
        result = previous(*args, **kwargs)
        try:
            info = send_due(args[0] if args else db, settings, now=kwargs.get("now"))
            if info.get("sent") or info.get("failed"):
                print(f"HR_PORTFOLIO_TELEGRAM_MAINT {info}", flush=True)
        except Exception as exc:
            error = sanitize_sensitive_text(exc)
            print(f"HR_PORTFOLIO_TELEGRAM_ERROR {type(exc).__name__}: {error}", flush=True)
            db.execute(
                "UPDATE hr_portfolio_telegram_state SET last_check_at=?,last_check_error=? WHERE id=1",
                (utc_now_iso(), error),
            )
        return result

    portfolio.run_maintenance = with_alerts

    @app.get("/api/hr-portfolio-telegram/status")
    def telegram_portfolio_status():
        return status(db, settings)

    tables = ("hr_portfolio_telegram_state", "hr_portfolio_telegram_alerts")
    try:
        import exporter
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES)+tables))
    except Exception:
        pass
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES)+tables))
        split_exporter.MANUAL_TABLES = tuple(dict.fromkeys(tuple(split_exporter.MANUAL_TABLES)+tables))
    except Exception:
        pass
    _INSTALLED = True
    return {
        "installed": True, "version": VERSION,
        "only_notification_source": ALLOWED_SOURCE,
        "legacy_senders_suppressed": True,
        "started_at": state["started_at"],
        "execution_authority": False,
        "extra_provider_calls": 0,
        "telegram_configured": bool(
            getattr(settings, "telegram_alerts_enabled", False) and
            getattr(settings, "telegram_bot_token", None) and
            getattr(settings, "telegram_chat_id", None)),
        "tables": tables,
    }
