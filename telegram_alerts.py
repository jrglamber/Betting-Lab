from __future__ import annotations

from datetime import datetime, timezone
import html
import json
from typing import Any, Dict, Mapping, Optional, Sequence
from urllib import parse, request

from db import Database, sanitize_sensitive_text, utc_now_iso


def _ensure_schema(db: Database) -> None:
    id_col = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute(
        f"""CREATE TABLE IF NOT EXISTS telegram_alert_state (
            singleton_id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            connection_test_sent_at TEXT
        )"""
    )
    try:
        db.execute("ALTER TABLE telegram_alert_state ADD COLUMN connection_test_sent_at TEXT")
    except Exception:
        pass
    db.execute(
        f"""CREATE TABLE IF NOT EXISTS telegram_alert_log (
            id {id_col},
            alert_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            channel TEXT NOT NULL,
            status TEXT NOT NULL,
            system_bet_id INTEGER,
            bookmaker_key TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            sent_at TEXT,
            last_error TEXT
        )"""
    )


def _state_start(db: Database) -> datetime:
    _ensure_schema(db)
    row = db.fetchone("SELECT started_at FROM telegram_alert_state WHERE singleton_id=1")
    if row:
        return datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
    stamp = utc_now_iso()
    db.execute("INSERT INTO telegram_alert_state(singleton_id,started_at) VALUES(1,?)", (stamp,))
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _format_kickoff(value: Any) -> str:
    raw = str(value or "")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        return dt.strftime("%d %b %H:%M UTC")
    except Exception:
        return raw or "—"


def format_heinz_message(card: Mapping[str, Any], legs: list[Mapping[str, Any]], *, test_mode: bool = True) -> str:
    banner = "🧪 SHADOW TEST — DO NOT PLACE" if test_mode else "🚨 HEINZ READY"
    book = str(card.get("bookmaker_title") or card.get("bookmaker_key") or "Bookmaker")
    spread = card.get("entry_quote_time_spread_minutes")
    lines = [
        banner,
        "",
        f"🎯 HEINZ · {book}",
        "6 selections · 57 lines · same bookmaker",
        f"Quote spread: {float(spread):.1f} min" if spread is not None else "Quote spread: —",
        "",
    ]
    for idx, leg in enumerate(legs, start=1):
        fixture = f"{leg.get('home_team') or ''} v {leg.get('away_team') or ''}".strip()
        selection = str(leg.get("selection") or "—")
        odds = float(leg.get("entry_odds") or 0.0)
        kickoff = _format_kickoff(leg.get("commence_time"))
        lines += [
            f"{idx}. {selection} @ {odds:.2f}",
            f"   {fixture} · {kickoff}",
        ]
    lines += [
        "",
        "💷 Stake: TEST ONLY — no stake assigned",
        f"Pool: {card.get('source_cohort') or '—'}",
        f"Card ID: {card.get('id')}",
        "",
        "This alert is forward-shadow evidence only. Prices can move; re-check every leg before any future manual placement.",
    ]
    return "\n".join(lines)


def format_cross_sport_message(
    card: Mapping[str, Any],
    legs: Sequence[Mapping[str, Any]],
    *,
    test_mode: bool = True,
) -> str:
    banner = "🧪 SHADOW TEST — DO NOT PLACE" if test_mode else "🚨 CROSS-SPORT SYSTEM READY"
    system_type = str(card.get("system_type") or "SYSTEM").upper()
    book = str((legs[0].get("bookmaker_key") if legs else None) or "Bookmaker")
    sports = ", ".join(sorted({str(x.get("sport_key") or "") for x in legs if x.get("sport_key")}))
    lines = [
        banner,
        "",
        f"🌍 CROSS-SPORT {system_type} · {book}",
        f"{len(legs)} selections · {card.get('line_count') or '—'} lines · same bookmaker",
        f"Sports: {sports or '—'}",
        "",
    ]
    for idx, leg in enumerate(legs, start=1):
        fixture = str(leg.get("fixture") or leg.get("event_id") or "—")
        selection = str(leg.get("selection") or "—")
        odds = float(leg.get("entry_odds") or 0.0)
        kickoff = _format_kickoff(leg.get("commence_time"))
        lines += [
            f"{idx}. {selection} @ {odds:.2f}",
            f"   {fixture} · {kickoff}",
        ]
    lines += [
        "",
        "💷 Stake: TEST ONLY — no stake assigned",
        f"Card ID: XS1-{card.get('id')}",
        "",
        "Forward-shadow evidence only. Re-check every price and market before any future manual placement.",
    ]
    return "\n".join(lines)


def _cross_sport_alert_legs(db: Database, system_bet_id: int) -> list[Dict[str, Any]]:
    rows = db.fetchall(
        """SELECT * FROM cross_sport_system_legs
           WHERE system_bet_id=? ORDER BY leg_order ASC""",
        (system_bet_id,),
    )
    out: list[Dict[str, Any]] = []
    for raw in rows:
        leg = dict(raw)
        table = "events" if str(leg.get("source_family") or "") == "FOOTBALL" else "multisport_events"
        try:
            event = db.fetchone(
                f"SELECT home_team,away_team,commence_time FROM {table} WHERE event_id=?",
                (leg["event_id"],),
            )
        except Exception:
            event = None
        if event:
            leg["fixture"] = f"{event.get('home_team') or ''} v {event.get('away_team') or ''}".strip()
            leg["commence_time"] = event.get("commence_time") or leg.get("commence_time")
        out.append(leg)
    return out


def _maybe_send_connection_test(db: Database, bot_token: str, chat_id: str) -> bool:
    _ensure_schema(db)
    row = db.fetchone(
        "SELECT connection_test_sent_at FROM telegram_alert_state WHERE singleton_id=1"
    ) or {}
    if row.get("connection_test_sent_at"):
        return False
    _send_telegram(
        bot_token,
        chat_id,
        "🧪 BETTING LAB TELEGRAM TEST\n\n"
        "Connection successful. Future qualifying multiples and cross-sport shadow alerts will arrive here.\n\n"
        "SHADOW TEST ONLY — DO NOT PLACE.",
    )
    db.execute(
        "UPDATE telegram_alert_state SET connection_test_sent_at=? WHERE singleton_id=1",
        (utc_now_iso(),),
    )
    return True


def _send_telegram(bot_token: str, chat_id: str, text: str) -> Dict[str, Any]:
    endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body = parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = request.Request(endpoint, data=body, method="POST")
    with request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not bool(payload.get("ok")):
        raise RuntimeError("Telegram API returned ok=false")
    return {"ok": True, "message_id": (payload.get("result") or {}).get("message_id")}


def send_pending_heinz_alerts(
    db: Database,
    settings: Any,
    *,
    algorithm_version: str,
    include_cross_sport: bool = True,
) -> Dict[str, Any]:
    """Send qualifying manual Heinz and XS1 cross-sport shadow alerts at most once."""
    enabled = bool(getattr(settings, "telegram_alerts_enabled", False))
    token = str(getattr(settings, "telegram_bot_token", "") or "").strip()
    chat_id = str(getattr(settings, "telegram_chat_id", "") or "").strip()
    test_mode = bool(getattr(settings, "telegram_test_mode", True))
    if not enabled:
        return {"enabled": False, "sent": 0, "reason": "disabled"}
    if not token or not chat_id:
        return {"enabled": True, "sent": 0, "reason": "credentials_missing"}

    start = _state_start(db)
    connection_test_sent = False
    try:
        connection_test_sent = _maybe_send_connection_test(db, token, chat_id)
    except Exception as exc:
        return {
            "enabled": True, "sent": 0, "reason": "connection_test_failed",
            "error": sanitize_sensitive_text(exc),
        }

    cards = db.fetchall(
        """SELECT * FROM manual_system_shadow_bets
           WHERE system_type='HEINZ'
             AND manual_placeable=1 AND status='OPEN'
             AND created_at>=?
             AND (algorithm_version=? OR algorithm_version='HP2_ODDS_4_TO_4_99_FORWARD')
           ORDER BY id ASC""",
        (start.isoformat(), algorithm_version),
    )
    manual_sent = manual_failed = manual_skipped = 0
    for card in cards:
        alert_key = f"telegram|heinz|{card['system_key']}"
        existing = db.fetchone("SELECT status FROM telegram_alert_log WHERE alert_key=?", (alert_key,))
        if existing and str(existing.get("status") or "") == "SENT":
            manual_skipped += 1
            continue
        legs = db.fetchall(
            """SELECT l.*,e.home_team,e.away_team,e.commence_time
               FROM manual_system_shadow_legs l
               JOIN events e ON e.event_id=l.event_id
               WHERE l.system_bet_id=? ORDER BY l.leg_order ASC""",
            (card["id"],),
        )
        if len(legs) != 6:
            manual_skipped += 1
            continue
        if not existing:
            db.execute(
                """INSERT INTO telegram_alert_log(
                     alert_key,created_at,channel,status,system_bet_id,bookmaker_key,attempts
                   ) VALUES(?,?,?,?,?,?,?)""",
                (alert_key, utc_now_iso(), "TELEGRAM", "PENDING", card["id"], card["bookmaker_key"], 0),
            )
        try:
            _send_telegram(token, chat_id, format_heinz_message(card, legs, test_mode=test_mode))
            db.execute(
                """UPDATE telegram_alert_log
                   SET status='SENT',attempts=attempts+1,sent_at=?,last_error=NULL
                   WHERE alert_key=?""",
                (utc_now_iso(), alert_key),
            )
            manual_sent += 1
        except Exception as exc:
            db.execute(
                """UPDATE telegram_alert_log
                   SET status='FAILED',attempts=attempts+1,last_error=?
                   WHERE alert_key=?""",
                (sanitize_sensitive_text(exc), alert_key),
            )
            manual_failed += 1

    cross_sent = cross_failed = cross_skipped = 0
    cross_cards = []
    if include_cross_sport:
        try:
            cross_cards = db.fetchall(
                """SELECT * FROM cross_sport_system_bets
                   WHERE status='OPEN' ORDER BY id ASC"""
            )
        except Exception:
            cross_cards = []
    for card in cross_cards:
        alert_key = f"telegram|cross_sport|{card['system_key']}"
        existing = db.fetchone("SELECT status FROM telegram_alert_log WHERE alert_key=?", (alert_key,))
        if existing and str(existing.get("status") or "") == "SENT":
            cross_skipped += 1
            continue
        legs = _cross_sport_alert_legs(db, int(card["id"]))
        if len(legs) != int(card.get("leg_count") or 0):
            cross_skipped += 1
            continue
        book = str((legs[0].get("bookmaker_key") if legs else None) or "")
        if not existing:
            db.execute(
                """INSERT INTO telegram_alert_log(
                     alert_key,created_at,channel,status,system_bet_id,bookmaker_key,attempts
                   ) VALUES(?,?,?,?,?,?,?)""",
                (alert_key, utc_now_iso(), "TELEGRAM", "PENDING", card["id"], book, 0),
            )
        try:
            _send_telegram(token, chat_id, format_cross_sport_message(card, legs, test_mode=test_mode))
            db.execute(
                """UPDATE telegram_alert_log
                   SET status='SENT',attempts=attempts+1,sent_at=?,last_error=NULL
                   WHERE alert_key=?""",
                (utc_now_iso(), alert_key),
            )
            cross_sent += 1
        except Exception as exc:
            db.execute(
                """UPDATE telegram_alert_log
                   SET status='FAILED',attempts=attempts+1,last_error=?
                   WHERE alert_key=?""",
                (sanitize_sensitive_text(exc), alert_key),
            )
            cross_failed += 1

    return {
        "enabled": True,
        "test_mode": test_mode,
        "eligible_manual_heinz": len(cards),
        "eligible_cross_sport": len(cross_cards),
        "manual_sent": manual_sent,
        "cross_sport_sent": cross_sent,
        "sent": manual_sent + cross_sent,
        "failed": manual_failed + cross_failed,
        "already_sent": manual_skipped + cross_skipped,
        "started_at": start.isoformat(),
        "connection_test_sent": connection_test_sent,
    }

