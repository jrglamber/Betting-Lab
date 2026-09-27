from datetime import datetime, timezone

from db import Database
from cohort_systems_shadow import (
    _line_combos, ensure_cohort_system_state, generate_cohort_system_shadows,
    settle_cohort_system_shadows, cohort_systems_scoreboard,
)


def _prediction(db, event_id, created, kickoff, i):
    db.execute("INSERT INTO events(event_id,sport_key,league,commence_time,home_team,away_team,first_seen_at,last_seen_at,status) VALUES(?,?,?,?,?,?,?,?,?)",
               (event_id,'soccer_test','Test League',kickoff,f'Home{i}',f'Away{i}',created,created,'UPCOMING'))
    db.execute("""INSERT INTO football_predictive_predictions(
        event_id,created_at,sport_key,league,commence_time,home_team,away_team,mapped_home_team,mapped_away_team,
        home_mapping_score,away_mapping_score,expected_home_goals,expected_away_goals,home_probability,draw_probability,away_probability,
        home_fair_odds,draw_fair_odds,away_fair_odds,league_training_matches,home_effective_matches,away_effective_matches,model_version,config_hash
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id,created,'soccer_test','Test League',kickoff,f'Home{i}',f'Away{i}',f'Home{i}',f'Away{i}',1,1,1.4,1.1,.45,.28,.27,2.22,3.57,3.70,50,8,8,'TEST','x'))
    return int(db.fetchone("SELECT id FROM football_predictive_predictions WHERE event_id=?",(event_id,))['id'])


def _add_btts(db, event_id, pred_id, created, idx):
    db.execute("""INSERT INTO football_predictive_market_predictions(
        prediction_id,event_id,created_at,market_key,selection,point,line_key,probability,fair_odds
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (pred_id,event_id,created,'btts','Yes',None,'btts:yes',.50,2.0))
    mp=int(db.fetchone("SELECT id FROM football_predictive_market_predictions WHERE event_id=? AND market_key='btts'",(event_id,))['id'])
    db.execute("""INSERT INTO football_predictive_market_bets(
        predictive_key,market_prediction_id,prediction_id,event_id,created_at,market_key,selection,point,line_key,bookmaker_key,bookmaker_title,
        offered_odds,model_probability,model_fair_odds,edge_pct,min_odds,strong_candidate,status,app_version,experiment_version,config_hash
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f'btts{idx}',mp,pred_id,event_id,created,'btts','Yes',None,'btts:yes','smarkets','Smarkets',2.2,.50,2.0,10,2.05,1,'OPEN','test','test','x'))


def _add_h2h(db, event_id, pred_id, created, idx):
    db.execute("""INSERT INTO football_predictive_bets(
        predictive_key,prediction_id,event_id,created_at,selection,bookmaker_key,bookmaker_title,offered_odds,model_probability,model_fair_odds,
        edge_pct,min_odds,strong_candidate,status,app_version,experiment_version,config_hash
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f'h2h{idx}',pred_id,event_id,created,f'Home{idx}','smarkets','Smarkets',4.5,.25,4.0,12.5,4.1,1,'OPEN','test','test','x'))


def test_line_counts():
    assert len(_line_combos('YANKEE',(1,2,3,4))) == 11
    assert len(_line_combos('HEINZ',(1,2,3,4,5,6))) == 57


def test_ms3_forward_formation_and_settlement(tmp_path):
    db=Database('',str(tmp_path/'ms3.sqlite'))
    db.init_schema()
    ensure_cohort_system_state(db)
    db.execute("UPDATE cohort_system_shadow_state SET started_at=? WHERE singleton_id=1",('2026-09-20T00:00:00+00:00',))
    created='2026-09-20T12:00:00+00:00'
    for i in range(1,13):
        kickoff=f'2026-09-21T{(i+6)%24:02d}:00:00+00:00'
        eid=f'e{i}'
        pid=_prediction(db,eid,created,kickoff,i)
        if i <= 6:
            _add_btts(db,eid,pid,created,i)
        else:
            _add_h2h(db,eid,pid,created,i)
    # v0.19.2 arms one prospective card at a time. A card is not counted
    # until every exact leg receives a post-arm quote. Simulate those fresh
    # approved-venue observations and advance through all six frozen experiments.
    t=datetime(2026,9,20,12,5,tzinfo=timezone.utc)
    assert generate_cohort_system_shadows(db,now=t) == 0
    assert cohort_systems_scoreboard(db)['armed'] == 1

    for step in range(6):
        arm=db.fetchone("SELECT * FROM cohort_system_shadow_bets WHERE status='ARMED' ORDER BY id LIMIT 1")
        assert arm is not None
        observed=t.replace(minute=t.minute + step*2 + 1)
        legs=db.fetchall("SELECT * FROM cohort_system_shadow_legs WHERE system_bet_id=? ORDER BY leg_order",(arm['id'],))
        for leg in legs:
            if leg['source_table']=='football_predictive_market_bets':
                table='football_predictive_market_price_observations'; fk='predictive_bet_id'
            elif leg['source_table']=='football_predictive_bets':
                table='football_predictive_price_observations'; fk='predictive_bet_id'
            else:
                raise AssertionError(leg['source_table'])
            stamp=observed.isoformat()+f"-{leg['id']}"
            # source_snapshot_at only needs to be unique per source in these test tables.
            db.execute(
                f"INSERT INTO {table}({fk},observed_at,source_snapshot_at,bookmaker_key,price,move_vs_entry_pct) VALUES(?,?,?,?,?,?)",
                (leg['source_id'],observed.isoformat(),stamp,'smarkets',float(leg['entry_odds']),0.0),
            )
        confirmed_at=observed.replace(minute=observed.minute + 1)
        assert generate_cohort_system_shadows(db,now=confirmed_at) == 1

    score=cohort_systems_scoreboard(db)
    assert score['cards'] == 6
    combos={x['label'] for x in score['segments']['cohort_system'] if x['cards']}
    assert 'BTTS · YANKEE' in combos and 'HYBRID · HEINZ' in combos

    # Settle source rows. Enough winners to exercise non-zero system returns.
    for row in db.fetchall("SELECT id FROM football_predictive_market_bets"):
        result='WIN' if int(row['id']) <= 5 else 'LOSS'
        db.execute("UPDATE football_predictive_market_bets SET status='SETTLED',result=?,closing_odds=2.1,clv_pct=4.5,clv_quality='A' WHERE id=?",(result,row['id']))
    for row in db.fetchall("SELECT id FROM football_predictive_bets"):
        result='WIN' if int(row['id']) % 2 else 'LOSS'
        db.execute("UPDATE football_predictive_bets SET status='SETTLED',result=?,closing_odds=4.2,clv_pct=7.1,clv_quality='A' WHERE id=?",(result,row['id']))
    settled=settle_cohort_system_shadows(db)
    assert settled == 6
    score=cohort_systems_scoreboard(db)
    assert score['settled'] == 6
    assert score['ab_clv_samples'] > 0
    assert db.fetchone("SELECT COUNT(*) n FROM cohort_system_shadow_lines WHERE result IS NOT NULL")['n'] > 100
