"""HR2-only Telegram routing: no real network calls or bookmaker bets."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from db import Database, utc_now_iso
import hit_rate_multiples_lab as hr
import hr2_telegram_only_patch as routing
import hr_portfolio_telegram
import telegram_alerts
import pytest


def _settings():
    return SimpleNamespace(
        telegram_alerts_enabled=True, telegram_bot_token="synthetic-token",
        telegram_chat_id="synthetic-chat", telegram_test_mode=False,
    )


def _db(tmp_path):
    db = Database(db_path=str(tmp_path/"hr2telegram.sqlite"))
    hr._ensure_schema(db)
    return db


def _now():
    return datetime.now(timezone.utc) + timedelta(seconds=3)


def _card(db, now, fmt="DOUBLE", *, book="williamhill",
          mode="SAME_BOOK_VERIFIED", policy=routing.POLICY_VERSION,
          exp="HR2", quote_age=2, kickoff_hours=4, same_fixture=False,
          market="h2h", point=None, status="OPEN", name=None):
    n = routing.FORMAT_LINES[fmt][0]
    name = name or f"c-{fmt}-{book}-{now.timestamp()}-{mode}-{exp}-{status}"
    db.execute("""INSERT INTO hit_rate_cards(
        card_key,policy_version,experiment_key,system_type,created_at,
        status,assembly_mode,reference_stake_gbp
    ) VALUES(?,?,?,?,?,?,?,?)""",
    (name, policy, exp, fmt, utc_now_iso(), status, mode, 10.0))
    card = db.fetchone("SELECT * FROM hit_rate_cards WHERE card_key=?", (name,))
    for i in range(n):
        event = "repeat" if same_fixture else f"{name}-e{i}"
        db.execute("""INSERT INTO hit_rate_legs(
            card_id,leg_order,candidate_id,source_lane,source_table,source_id,
            event_id,sport_key,market_key,selection,point,entry_odds,
            bookmaker_key,bookmaker_title,quote_captured_at,commence_time,
            home_team,away_team
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            int(card["id"]), i + 1, i + 1, "FOOTBALL_H2H_2_50_2_99",
            "test", i, event, "soccer_epl", market, f"Home {i}", point,
            2.75, book, routing.BOOKS.get(book,book),
            (now - timedelta(minutes=quote_age)).isoformat(),
            (now + timedelta(hours=kickoff_hours)).isoformat(),
            "Same Home" if same_fixture else f"Home {i}",
            "Same Away" if same_fixture else f"Away {i}",
        ))
    return card


def _capture(monkeypatch):
    sent = []
    def fake(_token, _chat, body, *, source=""):
        sent.append((body, source))
        return {"ok": True, "message_id": len(sent)}
    monkeypatch.setattr(telegram_alerts,"_send_telegram",fake)
    return sent


def test_outbound_rejects_all_sources_except_hr2_before_network():
    for source in ("", "HR_PORTFOLIO_SHADOW", "HR1", "LIVE_YANKEE",
                   "XS1", "TELEGRAM_DIAGNOSTIC"):
        with pytest.raises(RuntimeError,match="HR2_BET_READY_ONLY"):
            telegram_alerts._send_telegram("dummy", "dummy", "test", source=source)


@pytest.mark.parametrize("fmt,lines", [("DOUBLE",1)])
def test_hr2_placeable_format_notifies_once(tmp_path,monkeypatch,fmt,lines):
    db=_db(tmp_path)
    routing._schema(db); routing._state(db)
    now=_now()
    c=_card(db,now,fmt=fmt)
    sent=_capture(monkeypatch)
    first=routing.send_due(db,_settings(),now=now)
    assert first["sent"]==1 and first["uncertain"]==0
    assert len(sent)==1 and sent[0][1]=="HR2_BET_READY"
    body=sent[0][0]
    assert f"HR2 {fmt} — BET READY" in body
    assert f"{lines} line(s)" in body
    assert "William Hill" in body
    assert "MANUAL bet only" in body
    assert "NO bet has been placed" in body
    assert routing.send_due(db,_settings(),now=now)["sent"]==0
    assert db.fetchone(
        "SELECT status,attempts FROM hr2_telegram_alerts WHERE card_id=?",
        (int(c["id"]),)
    ) == {"status":"SENT","attempts":1}


@pytest.mark.parametrize("fmt", ["TRIXIE", "YANKEE", "HEINZ"])
def test_other_hr2_formats_keep_research_but_never_notify(tmp_path,monkeypatch,fmt):
    db=_db(tmp_path);routing._schema(db);routing._state(db);now=_now()
    c=_card(db,now,fmt=fmt,name=f"research-{fmt}")
    sent=_capture(monkeypatch)
    result=routing.send_due(db,_settings(),now=now)
    assert result["sent"]==0 and sent==[]
    assert db.fetchone("SELECT id,status FROM hit_rate_cards WHERE id=?", (c["id"],))["status"]=="OPEN"
    assert db.fetchone("SELECT card_id FROM hr2_telegram_alerts WHERE card_id=?", (c["id"],)) is None


def test_old_cards_and_non_hr2_cards_are_never_alerted(tmp_path,monkeypatch):
    db=_db(tmp_path);now=_now()
    old=_card(db,now,name="old")
    routing._schema(db);state=routing._state(db)
    assert state["activated_at"]
    _card(db,now,exp="HR1",name="hr1")
    _card(db,now,policy="old-version",name="hr2-old-policy")
    sent=_capture(monkeypatch)
    assert routing.send_due(db,_settings(),now=now)["sent"]==0
    assert sent==[]
    assert db.fetchone(
        "SELECT status FROM hr2_telegram_alerts WHERE card_id=?",
        (int(old["id"]),)
    )["status"]=="PRE_WIRE"
    assert routing._state(db)["activated_at"]==state["activated_at"]


@pytest.mark.parametrize("kwargs,reason", [
    ({"mode":"CROSS_BOOK_SYNTHETIC"},"not_placeable_same_book"),
    ({"book":"betfred_uk"},"bookmaker_not_supported"),
    ({"quote_age":20},"stale_quote"),
    ({"kickoff_hours":0.1},"kickoff_too_close"),
    ({"same_fixture":True},"duplicate_fixture"),
    ({"market":"totals"},"market_line_missing"),
    ({"status":"SETTLED"},"not_open"),
])
def test_unplaceable_bet_never_prompts(tmp_path,monkeypatch,kwargs,reason):
    db=_db(tmp_path);routing._schema(db);routing._state(db);now=_now()
    _card(db,now,name="no-bet",**kwargs)
    sent=_capture(monkeypatch)
    r=routing.send_due(db,_settings(),now=now)
    assert r["sent"]==0 and r["rejected"]==1
    assert sent==[]
    assert db.fetchone("SELECT error FROM hr2_telegram_alerts")["error"]==reason


def test_network_uncertainty_never_replays(tmp_path,monkeypatch):
    db=_db(tmp_path);routing._schema(db);routing._state(db);now=_now()
    c=_card(db,now,name="uncertain")
    tries=[]
    def failed(*args,**kwargs):
        tries.append(1)
        raise TimeoutError("network may have delivered Telegram")
    monkeypatch.setattr(telegram_alerts,"_send_telegram",failed)
    r=routing.send_due(db,_settings(),now=now)
    assert r["uncertain"]==1 and r["sent"]==0
    assert routing.send_due(db,_settings(),now=now)["sent"]==0
    assert len(tries)==1
    assert db.fetchone("SELECT status FROM hr2_telegram_alerts WHERE card_id=?",
                       (c["id"],))["status"]=="UNCERTAIN"


def test_card_formation_hook_routes_only_new_hr2_cards(tmp_path,monkeypatch):
    from fastapi import FastAPI
    db=_db(tmp_path); now=_now()
    app=FastAPI()
    sent=_capture(monkeypatch)
    old_form=hr._form
    old_pending=__import__("hit_rate_same_book_only_patch")._send_pending
    old_routing=hr_portfolio_telegram.ROUTING_ENABLED
    old_installed=routing._INSTALLED
    old_prev=routing._PREVIOUS_FORM
    try:
        def simulate_new_card(db_obj, time):
            _card(db_obj,time,fmt="DOUBLE",name="created-by-hr-form")
            return {"formed":1}
        monkeypatch.setattr(hr,"_form",simulate_new_card)
        routing._INSTALLED=False
        installed=routing.install(app,db,_settings())
        assert installed["only_notification_source"]=="HR2_BET_READY"
        assert hr_portfolio_telegram.ROUTING_ENABLED is False
        assert hr._form(db,now)=={"formed":1}
        assert len(sent)==1
        assert "HR2 DOUBLE" in sent[0][0]
        assert routing.status(db,_settings())["counts"][-1]["n"]>=1
    finally:
        hr._form=old_form
        __import__("hit_rate_same_book_only_patch")._send_pending=old_pending
        hr_portfolio_telegram.ROUTING_ENABLED=old_routing
        routing._INSTALLED=old_installed
        routing._PREVIOUS_FORM=old_prev


def test_status_contains_no_secrets(tmp_path):
    db=_db(tmp_path)
    out=routing.status(db,_settings())
    assert out["only_notification_source"]=="HR2_BET_READY"
    assert out["allowed_formats"]==["DOUBLE"]
    assert out["credentials_configured"]
    assert "synthetic-token" not in repr(out)
    assert "synthetic-chat" not in repr(out)
