"""Manual Telegram placement acknowledgements: secure, idempotent, no bookmaker actions."""
from types import SimpleNamespace

from db import Database
import hr2_telegram_only_patch as routing
import hr2_telegram_placement as placement


def _setup(tmp_path):
    db = Database(db_path=str(tmp_path / "placement.sqlite"))
    routing._schema(db)
    placement._schema(db)
    db.execute("""INSERT INTO hr2_telegram_alerts(
        card_id,card_key,format,status,created_at,telegram_message_id
    ) VALUES(?,?,?,?,?,?)""",
    (56, "synthetic-card-56", "DOUBLE", "SENT", "2026-11-06T12:00:00Z", "880"))
    db.execute("""INSERT INTO hr2_telegram_alerts(
        card_id,card_key,format,status,created_at,telegram_message_id
    ) VALUES(?,?,?,?,?,?)""",
    (57, "synthetic-yankee", "YANKEE", "SENT", "2026-11-06T12:00:00Z", "881"))
    settings = SimpleNamespace(telegram_chat_id="12345")
    return db, settings


def _update(text, *, update_id=99, reply_id=880, chat_id=12345,
            chat_type="private", author_id=12345):
    return {
        "update_id": update_id,
        "message": {
            "text": text,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": author_id},
            "reply_to_message": {"message_id": reply_id},
        },
    }


def test_plain_reply_records_placement_without_inventing_stake_or_odds(tmp_path):
    db,settings=_setup(tmp_path)
    assert placement.process_update(db,settings,_update("placed")) == {
        "status": "recorded", "card_id": 56, "priced": False,
    }
    row=db.fetchone("SELECT * FROM hr2_manual_placements WHERE card_id=56")
    assert row["reported_stake_gbp"] is None
    assert row["reported_bookmaker"] is None
    assert row["reported_leg1_odds"] is None
    assert row["reported_leg2_odds"] is None
    assert db.fetchone("SELECT status FROM hr2_telegram_alerts WHERE card_id=56")["status"]=="SENT"
    assert placement.summary(db)["manual_placements"]==1


def test_enrich_original_reply_and_duplicate_delivery_do_not_double_count(tmp_path):
    db,settings=_setup(tmp_path)
    placement.process_update(db,settings,_update("placed"))
    updated=placement.process_update(
        db,settings,_update("placed £10 WilliamHill 3.00 2.75",update_id=100))
    assert updated == {"status":"recorded","card_id":56,"priced":True}
    assert placement.process_update(
        db,settings,_update("placed £10 WilliamHill 3.00 2.75",update_id=100)
    )["status"]=="duplicate"
    row=db.fetchone("SELECT * FROM hr2_manual_placements WHERE card_id=56")
    assert row["reported_stake_gbp"]==10.0
    assert row["reported_bookmaker"]=="William Hill"
    assert row["reported_leg1_odds"]==3.0
    assert row["reported_leg2_odds"]==2.75
    assert placement.summary(db)["manual_placements"]==1
    assert placement.summary(db)["fully_priced"]==1


def test_wrong_chat_user_or_non_double_cannot_log(tmp_path):
    db,settings=_setup(tmp_path)
    for update in [
        _update("placed",chat_id=222),
        _update("placed",author_id=222),
        _update("placed",chat_type="group"),
        _update("placed",reply_id=881),
        _update("placed",reply_id=882),
        _update("won",reply_id=880),
    ]:
        assert placement.process_update(db,settings,update)["status"]!="recorded"
    assert placement.summary(db)["manual_placements"]==0


def test_plain_message_without_reply_never_guesses_card(tmp_path):
    db,settings=_setup(tmp_path)
    update=_update("placed")
    del update["message"]["reply_to_message"]
    assert placement.process_update(db,settings,update)["status"]=="reply_required"
    assert placement.summary(db)["manual_placements"]==0


def test_invalid_price_format_does_not_log(tmp_path):
    db,settings=_setup(tmp_path)
    for invalid in ["placed lots", "placed 10 unknown 3 2",
                    "placed 10 WilliamHill 0 2.75", "placed 10 WilliamHill 3"]:
        assert placement.process_update(db,settings,_update(invalid))["status"]=="invalid_details"
    assert placement.summary(db)["manual_placements"]==0


def test_webhook_secret_is_stable_and_credential_dependent():
    a=placement._webhook_secret("synthetic-token")
    b=placement._webhook_secret("different-token")
    assert a!=b and len(a)==64
    assert "synthetic-token" not in a
