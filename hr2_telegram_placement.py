from __future__ import annotations

"""Telegram replies confirming manual HR2 DOUBLE placement, not execution.

Only a Telegram private-chat reply to a previously SENT HR2 DOUBLE alert is
accepted. The reported stake and odds remain user-reported evidence, and
never alter research settlement, predictions or actual bookmaker accounts.
"""

from datetime import datetime, timezone
from hashlib import sha256
from threading import Thread
from urllib import parse, request
import json
import os
import re
import secrets

from db import sanitize_sensitive_text, utc_now_iso
from fastapi import HTTPException, Request
import telegram_alerts


WEBHOOK_PATH = "/api/hr2-telegram/webhook"
COMMAND = re.compile(r"^/?placed(?:@[A-Za-z0-9_]+)?(?:\s+|$)", re.IGNORECASE)
BOOKMAKERS = {
    "williamhill": "William Hill", "wh": "William Hill",
    "ladbrokes": "Ladbrokes", "ladbrokesuk": "Ladbrokes",
}


def _schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS hr2_manual_placements (
        card_id INTEGER PRIMARY KEY,
        telegram_message_id TEXT NOT NULL,
        source TEXT NOT NULL,
        first_confirmed_at TEXT NOT NULL,
        last_updated_at TEXT NOT NULL,
        last_update_id INTEGER NOT NULL,
        reported_stake_gbp REAL,
        reported_bookmaker TEXT,
        reported_leg1_odds REAL,
        reported_leg2_odds REAL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hr2_telegram_webhook_state (
        id INTEGER PRIMARY KEY,
        attempted_at TEXT,
        registered_at TEXT,
        last_error TEXT
    )""")


def _webhook_secret(token):
    # Telegram supplies this shared secret in its webhook request header.
    # Derive it from the existing bot credential, so no extra Railway secret
    # or publicly guessable endpoint token is needed.
    return sha256(("betting-lab-hr2-webhook|" + token).encode("utf-8")).hexdigest()


def _details(text):
    """Return stake, bookmaker and leg odds, or None for malformed input."""
    match = COMMAND.match(str(text or "").strip())
    if not match:
        return None
    tail = str(text).strip()[match.end():].split()
    if not tail:
        return (None, None, None, None)
    try:
        stake = float(tail[0].replace("£", ""))
        if not 0 < stake <= 10000:
            return None
        tail = tail[1:]
        if not tail:
            return (stake, None, None, None)
        book_key = re.sub(r"[^a-z]", "", tail[0].lower())
        if book_key == "william" and len(tail) > 1 and tail[1].lower() == "hill":
            book_key = "williamhill"
            tail = tail[1:]
        if book_key not in BOOKMAKERS:
            return None
        book = BOOKMAKERS[book_key]
        tail = tail[1:]
        if not tail:
            return (stake, book, None, None)
        if len(tail) != 2:
            return None
        odds1, odds2 = float(tail[0]), float(tail[1])
        if not (1.0 < odds1 <= 1000 and 1.0 < odds2 <= 1000):
            return None
        return (stake, book, odds1, odds2)
    except (ValueError, TypeError, IndexError):
        return None


def process_update(db, settings, update):
    """Process a Telegram update; reject unrelated chats or non-card replies."""
    _schema(db)
    message = update.get("message") or {}
    if not isinstance(message, dict):
        return {"status": "ignored"}
    chat = message.get("chat") or {}
    author = message.get("from") or {}
    configured_chat = str(getattr(settings, "telegram_chat_id", "") or "").strip()
    if (not configured_chat or str(chat.get("id")) != configured_chat
            or str(chat.get("type")) != "private"
            or str(author.get("id")) != configured_chat):
        return {"status": "unauthorized"}
    raw = str(message.get("text") or "").strip()
    if not COMMAND.match(raw):
        return {"status": "ignored"}
    reply_id = (message.get("reply_to_message") or {}).get("message_id")
    if reply_id is None:
        return {"status": "reply_required"}
    row = db.fetchone("""SELECT a.card_id,a.telegram_message_id
        FROM hr2_telegram_alerts a
        WHERE a.telegram_message_id=? AND a.format='DOUBLE'
          AND a.status='SENT' LIMIT 1""", (str(reply_id),))
    if row is None:
        return {"status": "unknown_double"}
    details = _details(raw)
    if details is None:
        return {"status": "invalid_details", "card_id": row["card_id"]}
    update_id = int(update.get("update_id") or 0)
    existing = db.fetchone(
        "SELECT * FROM hr2_manual_placements WHERE card_id=?",
        (int(row["card_id"]),),
    )
    if existing and int(existing["last_update_id"]) == update_id:
        return {"status": "duplicate", "card_id": int(row["card_id"])}
    stake, book, o1, o2 = details
    stamp = utc_now_iso()
    db.execute("""INSERT INTO hr2_manual_placements (
        card_id,telegram_message_id,source,first_confirmed_at,
        last_updated_at,last_update_id,reported_stake_gbp,
        reported_bookmaker,reported_leg1_odds,reported_leg2_odds
    ) VALUES (?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(card_id) DO UPDATE SET
        last_updated_at=excluded.last_updated_at,
        last_update_id=excluded.last_update_id,
        reported_stake_gbp=COALESCE(excluded.reported_stake_gbp,hr2_manual_placements.reported_stake_gbp),
        reported_bookmaker=COALESCE(excluded.reported_bookmaker,hr2_manual_placements.reported_bookmaker),
        reported_leg1_odds=COALESCE(excluded.reported_leg1_odds,hr2_manual_placements.reported_leg1_odds),
        reported_leg2_odds=COALESCE(excluded.reported_leg2_odds,hr2_manual_placements.reported_leg2_odds)
    """, (int(row["card_id"]), str(reply_id), "TELEGRAM_USER",
          stamp, stamp, update_id, stake, book, o1, o2))
    saved = db.fetchone("SELECT * FROM hr2_manual_placements WHERE card_id=?",
                        (int(row["card_id"]),))
    complete = all(saved.get(k) is not None for k in (
        "reported_stake_gbp", "reported_bookmaker",
        "reported_leg1_odds", "reported_leg2_odds"))
    return {"status": "recorded", "card_id": int(row["card_id"]),
            "priced": complete}


def _registration(db, token, domain):
    _schema(db)
    stamp = utc_now_iso()
    db.execute("""INSERT INTO hr2_telegram_webhook_state(id,attempted_at)
                  VALUES(1,?) ON CONFLICT(id) DO UPDATE SET attempted_at=excluded.attempted_at""", (stamp,))
    endpoint = "https://" + domain + WEBHOOK_PATH
    payload = json.dumps({
        "url": endpoint,
        "secret_token": _webhook_secret(token),
        "allowed_updates": ["message"],
        "drop_pending_updates": False,
    }).encode("utf-8")
    req = request.Request(
        "https://api.telegram.org/bot" + token + "/setWebhook",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=12) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError("Telegram rejected webhook registration")
        db.execute("""UPDATE hr2_telegram_webhook_state
            SET registered_at=?,last_error=NULL WHERE id=1""", (utc_now_iso(),))
        print("HR2_DOUBLE_TELEGRAM_WEBHOOK_REGISTERED", flush=True)
    except Exception as exc:
        reason = sanitize_sensitive_text(exc)
        db.execute("""UPDATE hr2_telegram_webhook_state
            SET last_error=? WHERE id=1""", (reason,))
        print("HR2_DOUBLE_TELEGRAM_WEBHOOK_FAILED: " + reason, flush=True)


def summary(db):
    _schema(db)
    row = db.fetchone("""SELECT COUNT(*) AS count,
        SUM(CASE WHEN reported_stake_gbp IS NOT NULL
                   AND reported_bookmaker IS NOT NULL
                   AND reported_leg1_odds IS NOT NULL
                   AND reported_leg2_odds IS NOT NULL
                 THEN 1 ELSE 0 END) AS fully_priced
        FROM hr2_manual_placements""") or {}
    hook = db.fetchone(
        "SELECT attempted_at,registered_at,last_error FROM hr2_telegram_webhook_state WHERE id=1"
    ) or {}
    return {
        "manual_placements": int(row.get("count") or 0),
        "fully_priced": int(row.get("fully_priced") or 0),
        "webhook_registered_at": hook.get("registered_at"),
        "webhook_registration_error": hook.get("last_error"),
        "no_auto_execution": True,
    }


def install(app, db, settings):
    _schema(db)

    @app.post(WEBHOOK_PATH)
    async def hr2_placement_webhook(incoming: Request):
        token = str(getattr(settings, "telegram_bot_token", "") or "").strip()
        supplied = incoming.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not token or not secrets.compare_digest(supplied, _webhook_secret(token)):
            raise HTTPException(status_code=403, detail="Forbidden")
        if int(incoming.headers.get("content-length") or 0) > 8192:
            raise HTTPException(status_code=413, detail="Payload too large")
        try:
            payload = await incoming.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Invalid update")
        result = process_update(db, settings, payload)
        if result["status"] == "recorded":
            card = result["card_id"]
            suffix = ("Stake, bookmaker and both odds recorded as user-reported."
                      if result["priced"] else
                      "Bet noted; stake/bookmaker/actual odds unverified. "
                      "Reply to the ORIGINAL double card with "
                      "'placed 10 WilliamHill 3.00 2.75' to add them.")
            try:
                telegram_alerts._send_telegram(
                    token, str(getattr(settings, "telegram_chat_id", "") or ""),
                    f"✅ HR2 DOUBLE #{card}: your manual placement is logged.\n"
                    f"{suffix}\nThis does not place or settle a bet.",
                    source="HR2_BET_READY",
                )
            except Exception as exc:
                print("HR2_DOUBLE_CONFIRMATION_SEND_FAILED: " +
                      sanitize_sensitive_text(exc), flush=True)
        return {"ok": True, "status": result["status"]}

    token = str(getattr(settings, "telegram_bot_token", "") or "").strip()
    domain = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    enabled = bool(getattr(settings, "telegram_alerts_enabled", False))
    if enabled and token and domain and re.fullmatch(r"[a-zA-Z0-9.-]+", domain):
        Thread(target=_registration, args=(db, token, domain), daemon=True).start()
    return {"installed": True, "method": "private_chat_reply_to_double",
            "webhook_registration_attempted": bool(enabled and token and domain),
            "no_auto_execution": True}
