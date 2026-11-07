from clv_hr2_shadow_research import analyze

def row(**kw):
    base = dict(evidence_mode="FORWARD", created_at="2026-11-01T10:00:00Z",
                commence_time="2026-11-01T20:00:00Z", event_id="e1",
                market_key="h2h", selection="home", point=None,
                sport_key="basketball_nba", source_lane="MULTISPORT_H2H",
                entry_odds=2.0, decision="ACCEPT", pnl_units=1.0)
    base.update(kw)
    return base

def test_exact_preexisting_match_only():
    hr = [dict(event_id="e1", market_key="h2h", selection="home",
               point=None, first_seen_at="2026-11-01T11:00:00Z"),
          dict(event_id="e1", market_key="h2h", selection="away",
               point=None, first_seen_at="2026-11-01T11:00:00Z")]
    result = analyze([row()], hr)
    assert result["hr2_overlap_counts"] == {"ACCEPT": 1, "NO_PRIOR_MATCH": 1}
    assert result["basketball"]["ACCEPT"]["pnl_units"] == 1.0

def test_no_future_leakage_or_backfill():
    hr = [dict(event_id="e1", market_key="h2h", selection="home",
               point=None, first_seen_at="2026-11-01T11:00:00Z")]
    result = analyze([row(created_at="2026-11-01T12:00:00Z"),
                      row(evidence_mode="BACKFILL")], hr)
    assert result["hr2_overlap_counts"]["NO_PRIOR_MATCH"] == 1

def test_line_and_match_start_guard():
    hr = [dict(event_id="e1", market_key="spreads", selection="home",
               point=-3.5, first_seen_at="2026-11-01T12:00:00Z")]
    result = analyze([row(market_key="spreads", point=-4.5),
                      row(created_at="2026-11-01T21:00:00Z")], hr)
    assert result["hr2_overlap_counts"]["NO_PRIOR_MATCH"] == 1
