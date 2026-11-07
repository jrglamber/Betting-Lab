from clv_basketball_replay import replay

def bet(event, created, start, pnl=None, odds=2.0, bookmaker="betfair_ex_uk"):
    return dict(evidence_mode="FORWARD", decision="ACCEPT", sport_key="BASKETBALL",
                event_id=event, market_key="h2h", selection=event,
                created_at=created, commence_time=start,
                entry_odds=odds, bookmaker_key=bookmaker, pnl_units=pnl,
                sample_key=event + bookmaker)

def test_unsettled_leg_is_not_removed_before_formation():
    rows = [bet("a", "2026-11-01T09:00:00Z", "2026-11-01T20:00:00Z", None),
            bet("b", "2026-11-01T10:00:00Z", "2026-11-01T21:00:00Z", 1)]
    result = replay(rows, sizes=(2,))
    assert result["results"]["doubles"]["cards"] == 1
    assert result["results"]["doubles"]["settled_cards"] == 0
    assert result["results"]["doubles"]["roi_pct"] is None

def test_distinct_events_and_same_book():
    rows = [bet("a", "2026-11-01T09:00:00Z", "2026-11-01T20:00:00Z", 1),
            bet("b", "2026-11-01T10:00:00Z", "2026-11-01T21:00:00Z", 1)]
    result = replay(rows, sizes=(2,))
    assert result["results"]["doubles"]["cards"] == 1
    assert result["results"]["doubles"]["pnl_units"] == 2.85

def test_no_cross_book_pairing():
    rows = [bet("a", "2026-11-01T09:00:00Z", "2026-11-01T20:00:00Z", 1),
            bet("b", "2026-11-01T10:00:00Z", "2026-11-01T21:00:00Z", 1,
                bookmaker="matchbook")]
    assert replay(rows, sizes=(2,))["results"]["doubles"]["cards"] == 0

def test_winning_single_commission():
    rows = [bet("a", "2026-11-01T09:00:00Z", "2026-11-01T20:00:00Z", 1)]
    result = replay(rows, sizes=(1,))
    assert result["results"]["singles"]["pnl_units"] == 0.95

def test_weekly_and_exchange_breakdowns_reconcile():
    rows = [bet("a", "2026-11-02T09:00:00Z", "2026-11-02T20:00:00Z", 1),
            bet("b", "2026-11-02T10:00:00Z", "2026-11-02T21:00:00Z", -1)]
    report = replay(rows, sizes=(1,))["results"]["singles"]
    assert report["settled_cards"] == 2
    assert sum(v["bets"] for v in report["by_week"].values()) == 2
    assert sum(v["pnl_units"] for v in report["by_exchange"].values()) == report["pnl_units"]
    assert report["pnl_units"] == -0.05

def test_same_event_cannot_be_double_counted():
    rows = [bet("a", "2026-11-02T09:00:00Z", "2026-11-02T20:00:00Z", 1),
            bet("a", "2026-11-02T10:00:00Z", "2026-11-02T20:00:00Z", 1)]
    assert replay(rows, sizes=(2,))["results"]["doubles"]["cards"] == 0
