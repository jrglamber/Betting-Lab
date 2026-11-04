"""HR Portfolio Shadow Telegram-only routing regression tests.

Mocks the outbound transport: tests never deliver a Telegram message.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db import Database, utc_now_iso
import hr_portfolio_shadow as portfolio
import hr_portfolio_telegram as alerts
import telegram_alerts


def _settings(enabled=True):
    return SimpleNamespace(
        telegram_alerts_enabled=enabled,
        telegram_bot_token="synthetic-token",
        telegram_chat_id="synthetic-chat",
        telegram_test_mode=False,
    )


def _now():
    return datetime.now(timezone.utc) + timedelta(seconds=2)


def _db(tmp_path):
    db = Database(db_path=str(tmp_path/"portfolio_telegram.sqlite"))
    portfolio._schema(db)
    alerts._schema(db)
    alerts._state(db)  # Activation before any newly formed card.
    return db


def _create(db, now, *, bookmaker="williamhill", key="card1", n=4, price_adjust=0.0,
            event_prefix="EV", source="FOOTBALL_H2H"):
    db.execute("""INSERT INTO hr_portfolio_groups(
        group_key,created_at,policy_version,cohort,bookmaker_key,
        selection_count,status,reference_stake_gbp
    ) VALUES(?,?,?,?,?,?,?,?)""", (
        key, utc_now_iso(), portfolio.VERSION, "UNIFIED",
        bookmaker, n, "OPEN", 10.0,
    ))
    group = db.fetchone("SELECT * FROM hr_portfolio_groups WHERE group_key=?", (key,))
    for i in range(1,n+1):
        quote = now-timedelta(minutes=2)
        kickoff = now+timedelta(hours=3+i)
        db.execute("""INSERT INTO hr_portfolio_legs(
            group_id,leg_order,source_lane,source_table,source_id,event_id,
            sport_key,market_key,selection,point,entry_odds,bookmaker_key,
            quote_captured_at,home_team,away_team,commence_time
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            group["id"], i, source, "multisport_execution_bets", i,
            f"{event_prefix}{i}", "americanfootball_ncaaf" if "NCAAF" in source else "soccer_epl",
            "h2h", f"Team {i}", None, 2.15+price_adjust,
            bookmaker, quote.isoformat(), f"Home {i}", f"Away {i}",
            kickoff.isoformat(),
        ))
    for fmt, fraction in portfolio._allocations(n).items():
        db.execute("""INSERT INTO hr_portfolio_formats(
            group_id,format,allocation_pct,status
        ) VALUES(?,?,?,?)""",
                   (group["id"], fmt, fraction, "OPEN"))
    return group


def _mock_transport(monkeypatch):
    sent = []
    def fake(token, chat, content, *, source=""):
        sent.append((token,chat,content,source))
        return {"ok":True,"message_id":len(sent)}
    monkeypatch.setattr(telegram_alerts, "_send_telegram", fake)
    return sent


def test_central_transport_denies_other_sources_without_network():
    with pytest.raises(RuntimeError, match="TELEGRAM_SOURCE_DISABLED"):
        telegram_alerts._send_telegram("dummy-token", "dummy-chat", "legacy Yankee alert")
    with pytest.raises(RuntimeError, match="TELEGRAM_SOURCE_DISABLED"):
        telegram_alerts._send_telegram("dummy-token", "dummy-chat", "diagnostic", source="DIAGNOSTIC")


def test_only_portfolio_new_group_alerted_once_with_full_manual_details(tmp_path,monkeypatch):
    db=_db(tmp_path); now=_now(); _create(db,now,n=4)
    sent=_mock_transport(monkeypatch)
    result=alerts.send_due(db,_settings(),now=now)
    assert result["sent"]==1
    assert len(sent)==1
    token,chat,msg,source=sent[0]
    assert source=="HR_PORTFOLIO_SHADOW"
    assert "William Hill ONLY" in msg
    assert "Match winner: Team 1" in msg
    assert "Home 1 v Away 1" in msg
    assert "£10.00 TOTAL across formats" in msg
    assert "Double (legs 1–2): £5.00 total" in msg
    assert "Trixie (legs 1–3): £3.00 total" in msg
    assert "Yankee (legs 1–4): £2.00 total" in msg
    assert "RECHECK bookmaker" in msg
    assert "SHADOW research alert" in msg
    assert alerts.send_due(db,_settings(),now=now)["sent"]==0
    assert len(sent)==1
    assert db.fetchone("SELECT status FROM hr_portfolio_telegram_alerts")["status"]=="SENT"


def test_same_selection_set_at_two_books_only_better_book_alerts(tmp_path,monkeypatch):
    db=_db(tmp_path); now=_now()
    _create(db,now,bookmaker="williamhill",key="w",n=3,price_adjust=0.0)
    _create(db,now,bookmaker="ladbrokes_uk",key="l",n=3,price_adjust=0.15)
    sent=_mock_transport(monkeypatch)
    r=alerts.send_due(db,_settings(),now=now)
    assert r["sent"]==1
    assert r["duplicates_suppressed"]==1
    assert len(sent)==1
    assert "Ladbrokes ONLY" in sent[0][2]
    assert "Double (legs 1–2): £6.00 total" in sent[0][2]
    assert "Trixie (legs 1–3): £4.00 total" in sent[0][2]
    counts={x["status"]:x["n"] for x in db.fetchall(
        "SELECT status,COUNT(*) AS n FROM hr_portfolio_telegram_alerts GROUP BY status")}
    assert counts=={"SENT":1,"SUPPRESSED_DUPLICATE":1}


def test_stale_odds_or_kickoff_too_close_never_alerted(tmp_path,monkeypatch):
    db=_db(tmp_path); now=_now()
    g=_create(db,now,key="stale",n=2)
    db.execute("UPDATE hr_portfolio_legs SET quote_captured_at=? WHERE group_id=?",
               ((now-timedelta(minutes=20)).isoformat(),g["id"]))
    sent=_mock_transport(monkeypatch)
    r=alerts.send_due(db,_settings(),now=now)
    assert r["sent"]==0
    assert not sent
    assert db.fetchone("SELECT status,last_error FROM hr_portfolio_telegram_alerts") == {
        "status":"EXPIRED", "last_error":"stale_or_future_quote"}


def test_preexisting_group_before_activation_not_notified(tmp_path,monkeypatch):
    db=Database(db_path=str(tmp_path/"existing.sqlite"))
    portfolio._schema(db)
    now=_now()
    _create(db,now,key="pre-existing",n=2)
    alerts._schema(db)
    alerts._state(db)
    sent=_mock_transport(monkeypatch)
    assert alerts.send_due(db,_settings(),now=now)["sent"]==0
    assert not sent


def test_no_config_no_sent_and_no_secrets_in_status(tmp_path,monkeypatch):
    db=_db(tmp_path); now=_now(); _create(db,now,n=2)
    sent=_mock_transport(monkeypatch)
    assert alerts.send_due(db,_settings(False),now=now)["sent"]==0
    assert not sent
    status=alerts.status(db,_settings())
    assert status["only_notification_source"]=="HR_PORTFOLIO_SHADOW"
    assert status["credentials_configured"] is True
    assert "synthetic-token" not in repr(status)


def test_failed_send_can_retry_if_quotes_still_fresh(tmp_path,monkeypatch):
    db=_db(tmp_path);now=_now();_create(db,now,n=2)
    calls=[]
    def sometimes(*args,**kwargs):
        calls.append(1)
        if len(calls)==1:
            raise RuntimeError("temporary provider rejection")
        return {"ok":True,"message_id":123}
    monkeypatch.setattr(telegram_alerts,"_send_telegram",sometimes)
    first=alerts.send_due(db,_settings(),now=now)
    assert first["sent"]==0 and first["failed"]==1
    second=alerts.send_due(db,_settings(),now=now+timedelta(seconds=1))
    assert second["sent"]==1 and second["failed"]==0
    assert len(calls)==2
    assert db.fetchone("SELECT status,attempts FROM hr_portfolio_telegram_alerts") == {
        "status":"SENT","attempts":2}


def test_never_send_automatically_when_delivery_is_ambiguous(tmp_path,monkeypatch):
    db=_db(tmp_path);now=_now();g=_create(db,now,n=2)
    db.execute("""INSERT INTO hr_portfolio_telegram_alerts(
        group_id,group_key,selection_signature,bookmaker_key,created_at,status,attempts
    ) VALUES(?,?,?,?,?,?,?)""",
               (g["id"],g["group_key"],"reserved","williamhill",utc_now_iso(),"SENDING",1))
    sent=_mock_transport(monkeypatch)
    assert alerts.send_due(db,_settings(),now=now)["sent"]==0
    assert not sent


def test_ncaaf_no_quota_and_two_leg_double_only(tmp_path,monkeypatch):
    db=_db(tmp_path);now=_now()
    _create(db,now,n=2,source="NCAAF_3_TO_4_99_EXPERIMENTAL")
    sent=_mock_transport(monkeypatch)
    r=alerts.send_due(db,_settings(),now=now)
    assert r["sent"]==1
    assert "NCAAF_3_TO_4_99_EXPERIMENTAL" in sent[0][2]
    assert "Double (legs 1–2): £10.00 total" in sent[0][2]
    assert "Trixie (legs 1–3)" not in sent[0][2]


def test_legacy_senders_are_all_muted(monkeypatch):
    # Tests direct sender functions are replaced independently of transport.
    import live_yankee_feed
    import manual_systems_shadow
    import web
    previous=(live_yankee_feed._send,manual_systems_shadow.send_pending_heinz_alerts,
              web.send_pending_heinz_alerts,web.send_telegram_test_now)
    try:
        alerts._disable_other_senders()
        assert live_yankee_feed._send(None,None)["sent"]==0
        assert manual_systems_shadow.send_pending_heinz_alerts(None,None,algorithm_version="x")["sent"]==0
        assert web.send_pending_heinz_alerts(None,None,algorithm_version="x")["sent"]==0
        assert web.send_telegram_test_now(None)["sent"]==0
    finally:
        (live_yankee_feed._send,manual_systems_shadow.send_pending_heinz_alerts,
         web.send_pending_heinz_alerts,web.send_telegram_test_now)=previous
