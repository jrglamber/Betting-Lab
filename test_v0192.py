from datetime import datetime, timedelta, timezone

from cohort_systems_shadow import (
    ensure_cohort_system_state,
    generate_cohort_system_shadows,
    cohort_systems_scoreboard,
)
from collector import Collector
from db import Database
from quota import QuotaGuard
from test_v0191 import _prediction, _add_btts, _add_h2h


class _NoCallApi:
    pass


def _seed(db, *, created, kickoff_base, btts=0, h2h=0):
    idx = 1
    for _ in range(btts):
        eid = f"b{idx}"
        kick = (kickoff_base + timedelta(minutes=idx * 10)).isoformat()
        pid = _prediction(db, eid, created, kick, idx)
        _add_btts(db, eid, pid, created, idx)
        idx += 1
    for _ in range(h2h):
        eid = f"h{idx}"
        kick = (kickoff_base + timedelta(minutes=idx * 10)).isoformat()
        pid = _prediction(db, eid, created, kick, idx)
        _add_h2h(db, eid, pid, created, idx)
        idx += 1


def test_ms3_armed_legs_are_priority_convergence_candidates(tmp_path):
    db = Database('', str(tmp_path / 'ms3priority.sqlite'))
    db.init_schema()
    ensure_cohort_system_state(db)
    db.execute(
        "UPDATE cohort_system_shadow_state SET started_at=? WHERE singleton_id=1",
        ('2026-09-20T00:00:00+00:00',),
    )
    created = '2026-09-20T12:00:00+00:00'
    now = datetime(2026, 9, 20, 12, 5, tzinfo=timezone.utc)
    _seed(db, created=created, kickoff_base=now + timedelta(hours=4), btts=4)

    assert generate_cohort_system_shadows(db, now=now) == 0
    arm = db.fetchone("SELECT * FROM cohort_system_shadow_bets WHERE status='ARMED'")
    assert arm is not None

    collector = Collector(
        db, _NoCallApi(), QuotaGuard(db, reserve=0, daily_budget=1000),
        sport_keys=['soccer_test'], region='uk', markets=['h2h', 'btts'],
        max_events_per_cycle=3, breadth_polls_per_day=0,
        execution_bookmaker_keys=['smarkets'],
    )
    rows = collector.convergence_candidates(now + timedelta(minutes=1))
    ms3 = [r for r in rows if int(r.get('ms3_priority') or 0) == 1]
    assert len(ms3) == 4
    assert all(r['market_key'] == 'btts' for r in ms3)
    assert all(r.get('ms3_armed_at') for r in ms3)


def test_ms3_rejects_arm_when_fresh_odds_leave_band(tmp_path):
    db = Database('', str(tmp_path / 'ms3reject.sqlite'))
    db.init_schema()
    ensure_cohort_system_state(db)
    db.execute(
        "UPDATE cohort_system_shadow_state SET started_at=? WHERE singleton_id=1",
        ('2026-09-20T00:00:00+00:00',),
    )
    created = '2026-09-20T12:00:00+00:00'
    now = datetime(2026, 9, 20, 12, 5, tzinfo=timezone.utc)
    _seed(db, created=created, kickoff_base=now + timedelta(hours=4), h2h=4)

    # With no BTTS pool, the first available frozen experiment is the odds Yankee.
    assert generate_cohort_system_shadows(db, now=now) == 0
    arm = db.fetchone("SELECT * FROM cohort_system_shadow_bets WHERE status='ARMED'")
    assert arm is not None
    assert arm['cohort_key'] == 'ODDS_4_TO_7_49'
    legs = db.fetchall(
        "SELECT * FROM cohort_system_shadow_legs WHERE system_bet_id=? ORDER BY leg_order",
        (arm['id'],),
    )
    observed = now + timedelta(minutes=2)
    for i, leg in enumerate(legs):
        price = 3.90 if i == 0 else 4.50
        db.execute(
            """INSERT INTO football_predictive_price_observations(
               predictive_bet_id,observed_at,source_snapshot_at,bookmaker_key,price,move_vs_entry_pct
               ) VALUES(?,?,?,?,?,?)""",
            (leg['source_id'], observed.isoformat(), f"fresh-{i}", 'smarkets', price, 0.0),
        )

    assert generate_cohort_system_shadows(db, now=observed + timedelta(minutes=1)) == 0
    rejected = db.fetchone(
        "SELECT * FROM cohort_system_shadow_bets WHERE id=?", (arm['id'],)
    )
    assert rejected['status'] == 'REJECTED'
    assert str(rejected['rejection_reason']) in {'leg_1_below_min_odds','leg_1_left_4_00_7_49_band'}
    score = cohort_systems_scoreboard(db)
    assert score['cards'] == 0
    assert score['rejected_arms'] == 1
