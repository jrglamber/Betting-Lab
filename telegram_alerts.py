from __future__ import annotations

from datetime import datetime, timezone
import html
import json
from typing import Any, Dict, Mapping, Optional
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
        "Connection successful. Future qualifying William Hill/Ladbrokes Heinz alerts will arrive here.\n\n"
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
) -> Dict[str, Any]:
    """Send each newly-created eligible frozen Heinz at most once.

    Enabling alerts establishes a forward-only boundary: cards created before
    that first enabled cycle are deliberately ignored, so switching the feature
    on cannot dump historical cards into Telegram.
    """
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
           WHERE algorithm_version=? AND system_type='HEINZ'
             AND manual_placeable=1 AND status='OPEN'
             AND created_at>=?
           ORDER BY id ASC""",
        (algorithm_version, start.isoformat()),
    )
    sent = failed = skipped = 0
    for card in cards:
        alert_key = f"telegram|heinz|{card['system_key']}"
        existing = db.fetchone("SELECT status FROM telegram_alert_log WHERE alert_key=?", (alert_key,))
        if existing and str(existing.get("status") or "") == "SENT":
            skipped += 1
            continue
        legs = db.fetchall(
            """SELECT l.*,e.home_team,e.away_team,e.commence_time
               FROM manual_system_shadow_legs l
               JOIN events e ON e.event_id=l.event_id
               WHERE l.system_bet_id=? ORDER BY l.leg_order ASC""",
            (card["id"],),
        )
        if len(legs) != 6:
            skipped += 1
            continue
        if not existing:
            db.execute(
                """INSERT INTO telegram_alert_log(
                     alert_key,created_at,channel,status,system_bet_id,bookmaker_key,attempts
                   ) VALUES(?,?,?,?,?,?,?)""",
                (alert_key, utc_now_iso(), "TELEGRAM", "PENDING", card["id"], card["bookmaker_key"], 0),
            )
        message = format_heinz_message(card, legs, test_mode=test_mode)
        try:
            _send_telegram(token, chat_id, message)
            db.execute(
                """UPDATE telegram_alert_log
                   SET status='SENT',attempts=attempts+1,sent_at=?,last_error=NULL
                   WHERE alert_key=?""",
                (utc_now_iso(), alert_key),
            )
            sent += 1
        except Exception as exc:
            db.execute(
                """UPDATE telegram_alert_log
                   SET status='FAILED',attempts=attempts+1,last_error=?
                   WHERE alert_key=?""",
                (sanitize_sensitive_text(exc), alert_key),
            )
            failed += 1
    return {
        "enabled": True,
        "test_mode": test_mode,
        "eligible": len(cards),
        "sent": sent,
        "failed": failed,
        "already_sent": skipped,
        "started_at": start.isoformat(),
        "connection_test_sent": connection_test_sent,
    }
