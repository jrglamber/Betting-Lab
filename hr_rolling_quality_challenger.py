from __future__ import annotations

"""Paired, forward-only price-quality challenger for the rolling HR portfolio.

The existing chronological V2 baseline has all selection authority. At each
new baseline group formation this challenger freezes the SAME eligible book,
fixture set, quote window, number of legs and reference stake. It ranks only
the choices actually observable before that decision. No retrospective picks.

No provider polling, wager execution, Telegram alerts, lane promotions or
updates to baseline rows. Price-quality is not calibrated winning probability.
"""

from datetime import datetime, timedelta, timezone
from html import escape
import hashlib
import json
from typing import Any

from fastapi.responses import HTMLResponse

import hit_rate_multiples_lab as hr
import hr_portfolio_shadow as baseline
import hr_market_integrity as integrity
from db import sanitize_sensitive_text, utc_now_iso

VERSION = "HR_ROLLING_QUALITY_CHALLENGER_V2"
QUOTE_WINDOW_MINUTES = 5.0
MAX_SNAPSHOT_CANDIDATES = 25
_INSTALLED = False


def _schema(db):
    ident = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute("""CREATE TABLE IF NOT EXISTS hr_quality_state (
        id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,version TEXT NOT NULL,
        rules_json TEXT NOT NULL,last_maintenance_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hr_quality_pairs (
        baseline_group_id INTEGER PRIMARY KEY,
        decision_at TEXT NOT NULL,bookmaker_key TEXT NOT NULL,
        baseline_policy_version TEXT NOT NULL,version TEXT NOT NULL,
        legs_n INTEGER NOT NULL,candidate_count INTEGER NOT NULL,
        ncaaf_pool_count INTEGER NOT NULL,baseline_ncaaf_count INTEGER NOT NULL,
        quality_ncaaf_count INTEGER NOT NULL,overlap_count INTEGER NOT NULL,
        candidate_digest TEXT,latest_quote_at TEXT,
        status TEXT NOT NULL,skip_reason TEXT,
        baseline_pnl_units REAL,quality_pnl_units REAL,delta_pnl_units REAL,
        settled_at TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hr_quality_legs (
        id {ident},baseline_group_id INTEGER NOT NULL,leg_order INTEGER NOT NULL,
        source_lane TEXT NOT NULL,source_table TEXT NOT NULL,source_id INTEGER NOT NULL,
        event_id TEXT NOT NULL,sport_key TEXT,market_key TEXT NOT NULL,
        selection TEXT NOT NULL,point REAL,entry_odds REAL NOT NULL,
        min_odds REAL NOT NULL,bookmaker_key TEXT NOT NULL,
        quote_captured_at TEXT NOT NULL,score REAL NOT NULL,score_json TEXT NOT NULL,
        home_team TEXT,away_team TEXT,commence_time TEXT,
        result TEXT,settled_at TEXT,
        UNIQUE(baseline_group_id,leg_order)
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hr_quality_formats (
        id {ident},baseline_group_id INTEGER NOT NULL,format TEXT NOT NULL,
        allocation_pct REAL NOT NULL,status TEXT NOT NULL,
        return_units REAL,pnl_units REAL,settled_at TEXT,
        UNIQUE(baseline_group_id,format)
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hr_quality_candidate_audit (
        id {ident},baseline_group_id INTEGER NOT NULL,rank INTEGER NOT NULL,
        source_lane TEXT NOT NULL,event_id TEXT NOT NULL,
        selection TEXT NOT NULL,price REAL NOT NULL,min_odds REAL NOT NULL,
        quote_at TEXT NOT NULL,score REAL NOT NULL,
        baseline_selected INTEGER NOT NULL,quality_selected INTEGER NOT NULL,
        components_json TEXT NOT NULL,
        UNIQUE(baseline_group_id,rank)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_hrq_status ON hr_quality_pairs(status,decision_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_hrq_legs_event ON hr_quality_legs(event_id)")


def _state(db):
    current = db.fetchone("SELECT * FROM hr_quality_state WHERE id=1")
    if current:
        if current["version"] != VERSION:
            rules = json.loads(current["rules_json"])
            rules.update({"version": VERSION, "matched_baseline": baseline.VERSION,
                          "match_baseline_fixture_set": True,
                          "competing_options": "rank one same-book alternative per fixture"})
            db.execute("UPDATE hr_quality_state SET version=?,rules_json=? WHERE id=1",
                       (VERSION, json.dumps(rules, sort_keys=True)))
            current = db.fetchone("SELECT * FROM hr_quality_state WHERE id=1")
        return current
    rules = {
        "version": VERSION, "matched_baseline": baseline.VERSION,
        "only_new_baseline_decisions": True,
        "quote_window_minutes": QUOTE_WINDOW_MINUTES,
        "same_bookmaker": True, "same_leg_count": True,
        "match_baseline_fixture_set": True,
        "competing_options": "rank one same-book alternative per fixture",
        "same_reference_stake_gbp": baseline.TOTAL_STAKE_GBP,
        "same_format_allocations": True, "no_ncaaf_quota": True,
        "no_live_authority": True, "additional_api_calls": 0,
        "score": {
            "frozen_minimum_odds_premium_pct_clipped_0_to_8": 0.55,
            "quote_freshness_fraction": 0.35,
            "approved_bookmaker_quote_coverage": 0.10,
        },
        "disclaimer": "Observed quote quality, not validated EV, CLV or win probability.",
    }
    stamp = utc_now_iso()
    db.execute("""INSERT INTO hr_quality_state(
        id,started_at,version,rules_json
    ) VALUES(1,?,?,?) ON CONFLICT(id) DO NOTHING""",
               (stamp, VERSION, json.dumps(rules, sort_keys=True)))
    return db.fetchone("SELECT * FROM hr_quality_state WHERE id=1")


def _dt(value):
    return hr._parse(value)


def _identity(candidate):
    return baseline._leg_identity(candidate)


def _price_score(candidate, quote, now, book_count):
    """Score only features visible at the decision point. Higher odds alone
    never score; only odds relative to the frozen source minimum do."""
    minimum = float(candidate.get("min_odds") or 0)
    price = float(quote.get("price") or 0)
    stamp = _dt(quote.get("captured_at"))
    if minimum <= 1 or price <= 1 or stamp is None or stamp > now:
        return None
    if price + 1e-12 < minimum:
        return None
    age = (now-stamp).total_seconds()/60.0
    if age < 0 or age > 15.0:
        return None
    premium_pct = max(0.0, min(8.0, (price/minimum-1)*100))
    premium = premium_pct/8.0
    freshness = max(0.0, 1.0-age/15.0)
    coverage = max(0.0, min(1.0, float(book_count)/2.0))
    score = 100.0*(0.55*premium + 0.35*freshness + 0.10*coverage)
    return {
        "score": round(score, 6),
        "premium_pct": round(premium_pct, 6),
        "quote_age_minutes": round(age, 6),
        "book_count": int(book_count),
        "component_price": round(55.0*premium, 6),
        "component_fresh": round(35.0*freshness, 6),
        "component_coverage": round(10.0*coverage, 6),
    }


def _rank_pool(items, all_book_options, now, earliest_quote, latest_quote, source_start):
    """Build a frozen same-book window; no later quotes or post-kickoff rows."""
    seen = set()
    records = []
    for candidate, quote in items:
        if not integrity.market_actionable(candidate):
            continue
        # Same economic selection can arrive under different provider IDs;
        # rank it once rather than awarding it multiple apparent positions.
        key = integrity.selection_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        stamp = _dt(quote.get("captured_at"))
        kickoff = _dt(candidate.get("commence_time"))
        if not stamp or not kickoff:
            continue
        if not (source_start <= stamp <= latest_quote):
            continue
        if not (earliest_quote <= stamp <= latest_quote):
            continue
        if kickoff <= now+timedelta(minutes=baseline.MIN_LEAD_MINUTES):
            continue
        book_count = sum(
            any(integrity.selection_key(other_c) == key and
                (earliest_quote <= _dt(other_q.get("captured_at")) <= latest_quote)
                for other_c, other_q in book_items
                if _dt(other_q.get("captured_at")) is not None)
            for book_items in all_book_options.values()
        )
        detail = _price_score(candidate, quote, now, book_count)
        if detail is None:
            continue
        records.append({
            "candidate": dict(candidate), "quote": dict(quote),
            "key": key, "detail": detail,
        })
    records.sort(key=lambda x: (
        -float(x["detail"]["score"]),
        str(x["candidate"].get("source_created_at") or x["candidate"].get("first_seen_at") or ""),
        str(x["candidate"].get("event_id") or ""),
        str(x["candidate"].get("source_lane") or ""),
    ))
    return records


def _select(ranked, count):
    chosen, events = [], set()
    for item in ranked:
        candidate = item["candidate"]
        event = str(candidate.get("event_id") or "")
        fixture = integrity.fixture_key(candidate)
        if not event or fixture in events or not integrity.market_actionable(candidate):
            continue
        chosen.append(item)
        events.add(fixture)
        if len(chosen) == count:
            break
    return chosen if len(chosen) == count else []


def _select_same_fixtures(ranked, baseline_legs):
    """Rank competing options within each frozen baseline fixture.

    Fixing the fixture set isolates the effect of market-choice ranking from
    fixture discovery. Alternatives remain in the audit; no multiple contains
    two legs from the same real match, even across differing provider IDs.
    """
    best_by_fixture = {}
    for item in ranked:
        fixture = integrity.fixture_key(item["candidate"])
        best_by_fixture.setdefault(fixture, item)  # globally ranked high to low
    chosen, used = [], set()
    for leg in baseline_legs:
        fixture = integrity.fixture_key(leg)
        if fixture in used or fixture not in best_by_fixture:
            return []
        chosen.append(best_by_fixture[fixture])
        used.add(fixture)
    return chosen


def _skip(db, group, reason, baseline_ncaaf=0):
    db.execute("""INSERT INTO hr_quality_pairs(
        baseline_group_id,decision_at,bookmaker_key,baseline_policy_version,
        version,legs_n,candidate_count,ncaaf_pool_count,baseline_ncaaf_count,
        quality_ncaaf_count,overlap_count,status,skip_reason
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(baseline_group_id) DO NOTHING""", (
        int(group["id"]), group["created_at"], group["bookmaker_key"],
        group["policy_version"], VERSION, int(group["selection_count"]), 0, 0,
        int(baseline_ncaaf), 0, 0, "SKIPPED", reason,
    ))


def _pair(db, group, options, challenger_start, now):
    gid = int(group["id"])
    if db.fetchone("SELECT baseline_group_id FROM hr_quality_pairs WHERE baseline_group_id=?", (gid,)):
        return "duplicate"
    prior = [dict(x) for x in db.fetchall(
        "SELECT * FROM hr_portfolio_legs WHERE group_id=? ORDER BY leg_order", (gid,)
    )]
    count = int(group["selection_count"])
    base_ncaaf = sum(str(x.get("source_lane") or "").startswith("NCAAF_") for x in prior)
    if len(prior) != count or count not in (2, 3, 4):
        _skip(db, group, "INCOMPLETE_BASELINE", base_ncaaf)
        return "skipped"
    decision = _dt(group["created_at"])
    stamps = [_dt(x.get("quote_captured_at")) for x in prior]
    if not decision or not all(stamps):
        _skip(db, group, "MISSING_DECISION_TIME", base_ncaaf)
        return "skipped"
    earliest, latest = min(stamps), max(stamps)
    if latest > decision or earliest < challenger_start:
        _skip(db, group, "PRE_ACTIVATION_OR_FUTURE_QUOTE", base_ncaaf)
        return "skipped"
    if (latest-earliest).total_seconds() > QUOTE_WINDOW_MINUTES*60+1e-8:
        _skip(db, group, "BASELINE_NOT_QUOTE_COHERENT", base_ncaaf)
        return "skipped"

    book = str(group["bookmaker_key"])
    pool = options.get(book, [])
    ranked = _rank_pool(
        pool, options, now, latest-timedelta(minutes=QUOTE_WINDOW_MINUTES),
        latest, challenger_start,
    )
    # Verify that the freshly read same-book snapshot reproduces all baseline
    # prices EXACTLY; if anything has changed, comparison would be unfair.
    for leg in prior:
        matched = [
            item for item in ranked
            if _identity(item["candidate"]) == _identity(leg)
            and float(item["quote"]["price"]) == float(leg["entry_odds"])
            and str(item["quote"]["captured_at"]) == str(leg["quote_captured_at"])
        ]
        if not matched:
            _skip(db, group, "BASELINE_QUOTE_NOT_IN_SAME_SNAPSHOT", base_ncaaf)
            return "skipped"
    selected = _select_same_fixtures(ranked, prior)
    if len(selected) != count:
        _skip(db, group, "MISSING_MATCHED_FIXTURE_OPTION", base_ncaaf)
        return "skipped"
    fixture_counts = {}
    for item in ranked:
        key = integrity.fixture_key(item["candidate"])
        fixture_counts[key] = fixture_counts.get(key, 0) + 1
    for item in ranked:
        item["detail"]["same_fixture_eligible_options"] = fixture_counts[
            integrity.fixture_key(item["candidate"])]

    baseline_keys = {_identity(x) for x in prior}
    selected_keys = {_identity(x["candidate"]) for x in selected}
    overlap = len(baseline_keys & selected_keys)
    ncaaf_pool = sum(str(x["candidate"].get("source_lane") or "").startswith("NCAAF_") for x in ranked)
    selected_ncaaf = sum(str(x["candidate"].get("source_lane") or "").startswith("NCAAF_") for x in selected)
    digest = hashlib.sha256(json.dumps([
        (x["key"], x["quote"].get("captured_at"),
         round(float(x["quote"]["price"]), 8),
         round(float(x["detail"]["score"]), 6))
        for x in ranked
    ], sort_keys=True, default=str).encode("utf-8")).hexdigest()

    db.execute("""INSERT INTO hr_quality_pairs(
        baseline_group_id,decision_at,bookmaker_key,baseline_policy_version,
        version,legs_n,candidate_count,ncaaf_pool_count,baseline_ncaaf_count,
        quality_ncaaf_count,overlap_count,candidate_digest,latest_quote_at,status
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(baseline_group_id) DO NOTHING""", (
        gid, group["created_at"], book, group["policy_version"], VERSION, count,
        len(ranked), ncaaf_pool, base_ncaaf, selected_ncaaf, overlap,
        digest, latest.isoformat(), "OPEN",
    ))
    for i, item in enumerate(selected, 1):
        c, q, d = item["candidate"], item["quote"], item["detail"]
        db.execute("""INSERT INTO hr_quality_legs(
            baseline_group_id,leg_order,source_lane,source_table,source_id,event_id,
            sport_key,market_key,selection,point,entry_odds,min_odds,bookmaker_key,
            quote_captured_at,score,score_json,home_team,away_team,commence_time
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(baseline_group_id,leg_order) DO NOTHING""", (
            gid, i, str(c["source_lane"]), str(c["source_table"]), int(c["source_id"]),
            str(c["event_id"]), c.get("sport_key"), str(c["market_key"]),
            str(c["selection"]), c.get("point"), float(q["price"]),
            float(c["min_odds"]), book, str(q["captured_at"]), float(d["score"]),
            json.dumps(d, sort_keys=True), c.get("home_team"), c.get("away_team"),
            c.get("commence_time"),
        ))
    for format_name, share in baseline._allocations(count).items():
        db.execute("""INSERT INTO hr_quality_formats(
            baseline_group_id,format,allocation_pct,status
        ) VALUES(?,?,?,?) ON CONFLICT(baseline_group_id,format) DO NOTHING""",
                   (gid, format_name, float(share), "OPEN"))

    for i, item in enumerate(ranked[:MAX_SNAPSHOT_CANDIDATES], 1):
        c, q, d = item["candidate"], item["quote"], item["detail"]
        db.execute("""INSERT INTO hr_quality_candidate_audit(
            baseline_group_id,rank,source_lane,event_id,selection,price,min_odds,
            quote_at,score,baseline_selected,quality_selected,components_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(baseline_group_id,rank) DO NOTHING""", (
            gid, i, str(c["source_lane"]), str(c["event_id"]), str(c["selection"]),
            float(q["price"]), float(c["min_odds"]), str(q["captured_at"]),
            float(d["score"]), int(_identity(c) in baseline_keys),
            int(_identity(c) in selected_keys), json.dumps(d, sort_keys=True),
        ))
    return "formed"


def _form_wrapper(previous_form):
    def paired_form(db, now, state):
        _schema(db)
        info = _state(db)
        started = _dt(info["started_at"])
        before = {
            int(x["id"]) for x in db.fetchall(
                "SELECT id FROM hr_portfolio_groups WHERE policy_version=? AND created_at>=?",
                (baseline.VERSION, info["started_at"]),
            )
        }
        result = previous_form(db, now, state)
        new_groups = [dict(x) for x in db.fetchall(
            "SELECT * FROM hr_portfolio_groups WHERE policy_version=? AND created_at>=? ORDER BY id",
            (baseline.VERSION, info["started_at"]),
        ) if int(x["id"]) not in before]
        if not new_groups:
            return result
        # Match exactly the same canonical HR2 pool as the baseline; do
        # not introduce any portfolio-only NCAAF source to this arm.
        sources = baseline._base_candidates(db, now)
        statuses = {
            str(x["source_lane"]): str(x["status"]).upper()
            for x in db.fetchall("SELECT source_lane,status FROM hr_portfolio_lane_policy")
        }
        eligible = [c for c in sources
                    if statuses.get(str(c.get("source_lane") or "")) == "ELIGIBLE"]
        # Keep every quoted alternative in the common HR2 source pool.
        # Each paired decision ranks within the baseline's frozen fixtures.
        options = baseline._book_options(db, eligible, now, _dt(state["started_at"]))
        formed = skipped = 0
        for g in new_groups:
            try:
                outcome = _pair(db, g, options, started, now)
                formed += int(outcome == "formed")
                skipped += int(outcome == "skipped")
            except Exception as exc:
                print("HR_QUALITY_PAIR_ERROR " + sanitize_sensitive_text(
                    f"{g.get('id')}:{type(exc).__name__}:{exc}"
                ), flush=True)
                _skip(db, g, f"PAIR_ERROR:{type(exc).__name__}")
                skipped += 1
        if formed or skipped:
            print(f"HR_QUALITY_PAIR_CREATED formed={formed} skipped={skipped}", flush=True)
        return result
    return paired_form


def _settle(db):
    settled = 0
    for pair in db.fetchall("SELECT * FROM hr_quality_pairs WHERE status='OPEN' ORDER BY baseline_group_id"):
        gid = int(pair["baseline_group_id"])
        legs = [dict(x) for x in db.fetchall(
            "SELECT * FROM hr_quality_legs WHERE baseline_group_id=? ORDER BY leg_order", (gid,)
        )]
        if len(legs) != int(pair["legs_n"]):
            continue
        for leg in legs:
            if hr._result(leg.get("result")):
                continue
            res = hr._source_result(db, leg)
            if res:
                db.execute("UPDATE hr_quality_legs SET result=?,settled_at=? WHERE id=?",
                           (res, utc_now_iso(), int(leg["id"])))
                leg["result"] = res
        if any(hr._result(x.get("result")) is None for x in legs):
            continue
        base_formats = {
            str(x["format"]): dict(x)
            for x in db.fetchall(
                "SELECT * FROM hr_portfolio_formats WHERE group_id=?", (gid,)
            )
        }
        entries = [dict(x) for x in db.fetchall(
            "SELECT * FROM hr_quality_formats WHERE baseline_group_id=?", (gid,)
        )]
        if not entries or any(
            f.get("format") not in base_formats or
            base_formats[f["format"]].get("pnl_units") is None
            for f in entries
        ):
            continue  # Don't declare a result until BOTH paired arms settle.
        results = [dict(x, result=hr._result(x.get("result"))) for x in legs]
        q_pnl = b_pnl = 0.0
        for f in entries:
            return_units = baseline._return_for(f["format"], results)
            if return_units is None:
                continue
            p = float(return_units)-1.0
            allocation = float(f["allocation_pct"])
            q_pnl += allocation*p
            b_pnl += allocation*float(base_formats[f["format"]]["pnl_units"])
            db.execute("""UPDATE hr_quality_formats SET return_units=?,pnl_units=?,
                status='SETTLED',settled_at=? WHERE id=?""",
                (float(return_units), p, utc_now_iso(), int(f["id"])))
        db.execute("""UPDATE hr_quality_pairs SET status='SETTLED',
            baseline_pnl_units=?,quality_pnl_units=?,delta_pnl_units=?,settled_at=?
            WHERE baseline_group_id=?""",
                   (b_pnl, q_pnl, round(q_pnl-b_pnl, 8), utc_now_iso(), gid))
        settled += 1
    return settled


def _run_wrapper(previous_run):
    def with_quality(db, now=None):
        result = previous_run(db, now=now)
        try:
            count = _settle(db)
            if count:
                print(f"HR_QUALITY_SETTLED paired_groups={count}", flush=True)
            db.execute("UPDATE hr_quality_state SET last_maintenance_at=? WHERE id=1",
                       (utc_now_iso(),))
            print("HR_QUALITY_MAINT " + json.dumps({"settled_pairs": count, "baseline_formed": result.get("formed", 0) if isinstance(result, dict) else None, "at": utc_now_iso()}, sort_keys=True), flush=True)
        except Exception as exc:
            error = sanitize_sensitive_text(exc)
            print(f"HR_QUALITY_SETTLE_ERROR {type(exc).__name__}: {error}", flush=True)
            try:
                db.record_collector_run("HR_QUALITY_SETTLEMENT", False, detail=error)
            except Exception:
                pass
        return result
    return with_quality


def scoreboard(db):
    _schema(db)
    state = db.fetchone("SELECT * FROM hr_quality_state WHERE id=1") or {}
    # Keep pre-change ranking evidence archived, but never mix policy V1
    # with the new fixture-matched V2 experiment in performance statistics.
    pairs = [dict(x) for x in db.fetchall(
        "SELECT * FROM hr_quality_pairs WHERE version=? ORDER BY baseline_group_id",
        (VERSION,),
    )]
    done = [p for p in pairs if p["status"] == "SETTLED"]
    skipped = {}
    for p in pairs:
        if p["status"] == "SKIPPED":
            reason = str(p.get("skip_reason") or "OTHER")
            skipped[reason] = skipped.get(reason, 0)+1
    b_total = sum(float(x["baseline_pnl_units"]) for x in done)
    q_total = sum(float(x["quality_pnl_units"]) for x in done)
    delta = q_total-b_total
    format_rows = db.fetchall("""SELECT q.format,COUNT(*) AS settled,
        SUM(q.pnl_units) AS quality_pnl_units FROM hr_quality_formats q
        JOIN hr_quality_pairs p ON p.baseline_group_id=q.baseline_group_id
        WHERE q.status='SETTLED' AND p.version=?
        GROUP BY q.format ORDER BY q.format""", (VERSION,))
    base_by_format = {}
    for row in db.fetchall("""SELECT b.format,COUNT(*) AS settled,
        SUM(b.pnl_units) AS baseline_pnl_units
        FROM hr_quality_pairs p JOIN hr_portfolio_formats b
          ON p.baseline_group_id=b.group_id
        WHERE p.status='SETTLED' AND b.status='SETTLED' AND p.version=?
        GROUP BY b.format ORDER BY b.format""", (VERSION,)):
        base_by_format[str(row["format"])] = dict(row)
    formats = []
    for row in format_rows:
        r = dict(row)
        old = base_by_format.get(str(r["format"]), {})
        r["baseline_pnl_units"] = old.get("baseline_pnl_units")
        r["delta_pnl_units"] = (
            float(r.get("quality_pnl_units") or 0) -
            float(old.get("baseline_pnl_units") or 0)
        )
        formats.append(r)
    latest = db.fetchall("""SELECT p.baseline_group_id,p.decision_at,
        p.bookmaker_key,p.legs_n,p.candidate_count,p.overlap_count,
        p.baseline_ncaaf_count,p.quality_ncaaf_count,p.status,
        p.baseline_pnl_units,p.quality_pnl_units,p.delta_pnl_units,p.skip_reason
        FROM hr_quality_pairs p WHERE p.version=?
        ORDER BY p.baseline_group_id DESC LIMIT 25""", (VERSION,))
    matched_doubles = [dict(r) for r in db.fetchall("""
        SELECT p.baseline_group_id,b.pnl_units AS baseline_pnl,
               q.pnl_units AS quality_pnl
        FROM hr_quality_pairs p
        JOIN hr_portfolio_formats b ON b.group_id=p.baseline_group_id
        JOIN hr_quality_formats q ON q.baseline_group_id=p.baseline_group_id
        WHERE p.version=? AND p.status='SETTLED'
          AND b.format='DOUBLE' AND q.format='DOUBLE'
          AND b.status='SETTLED' AND q.status='SETTLED'
        ORDER BY p.baseline_group_id""", (VERSION,))]

    def double_stats(field):
        values = [float(r[field]) for r in matched_doubles]
        running = peak = drawdown = 0.0
        for pnl in values:
            running += pnl
            peak = max(peak, running)
            drawdown = max(drawdown, peak-running)
        n = len(values)
        return {
            "settled": n,
            "wins": sum(pnl > 1e-10 for pnl in values),
            "hit_rate_pct": round(100*sum(pnl > 1e-10 for pnl in values)/n, 3) if n else None,
            "roi_pct": round(100*sum(values)/n, 3) if n else None,
            "max_drawdown_units": round(drawdown, 4),
        }

    baseline_groups = (db.fetchone(
        "SELECT COUNT(*) AS n FROM hr_portfolio_groups WHERE policy_version=?",
        (baseline.VERSION,),
    ) or {}).get("n") or 0
    paired_decisions = [p for p in pairs if p["status"] != "SKIPPED"]
    return {
        "version": VERSION, "research_only": True, "no_provider_calls": True,
        "baseline_groups_formed": int(baseline_groups),
        "quality_groups_formed": len(paired_decisions),
        "ranked_changed_groups": sum(
            int(p["overlap_count"]) < int(p["legs_n"]) for p in paired_decisions),
        "ranked_displaced_legs": sum(
            max(0, int(p["legs_n"])-int(p["overlap_count"])) for p in paired_decisions),
        "matched_doubles": {
            "baseline": double_stats("baseline_pnl"),
            "quality": double_stats("quality_pnl"),
        },
        "started_at": state.get("started_at"),
        "last_maintenance_at": state.get("last_maintenance_at"),
        "scoring_rules": json.loads(state.get("rules_json") or "{}"),
        "paired": len(done), "pending": sum(x["status"] == "OPEN" for x in pairs),
        "skipped": skipped, "total_decisions": len(pairs),
        "baseline_pnl_units": round(b_total, 6),
        "quality_pnl_units": round(q_total, 6),
        "paired_delta_pnl_units": round(delta, 6),
        "matched_roi_difference_pct": round(delta/len(done)*100.0, 6) if done else None,
        "quality_wins": sum(float(x["delta_pnl_units"]) > 1e-10 for x in done),
        "baseline_wins": sum(float(x["delta_pnl_units"]) < -1e-10 for x in done),
        "same_result": sum(abs(float(x["delta_pnl_units"])) <= 1e-10 for x in done),
        "average_overlap_legs": (
            sum(int(x["overlap_count"]) for x in done)/len(done)
            if done else None
        ),
        "formats": formats, "latest": [dict(x) for x in latest],
        "note": "Same decision/quote window/book and reference stake; price quality is a proxy, not expected value.",
    }


def install(app, db):
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    if not baseline._INSTALLED:
        raise RuntimeError("Baseline HR portfolio must install before quality challenger")
    baseline._form = _form_wrapper(baseline._form)
    baseline.run_maintenance = _run_wrapper(baseline.run_maintenance)

    @app.get("/api/hr-quality-challenger/status")
    def quality_status():
        return scoreboard(db)

    @app.get("/hr-quality-challenger", response_class=HTMLResponse)
    def quality_page():
        from hr_portfolio_presentation import render_quality
        from hr_portfolio_queues import read as read_queue
        return HTMLResponse(render_quality(scoreboard(db), queue=read_queue(db, view='quality')))

    # Native exports retain every paired decision, feature snapshot and result.
    tables = ("hr_quality_state", "hr_quality_pairs", "hr_quality_legs",
              "hr_quality_formats", "hr_quality_candidate_audit")
    try:
        import exporter
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + tables))
    except Exception as exc:
        print("HR_QUALITY_FULL_EXPORT_ERROR " + sanitize_sensitive_text(exc), flush=True)
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + tables))
        split_exporter.MANUAL_TABLES = tuple(dict.fromkeys(tuple(split_exporter.MANUAL_TABLES) + tables))
    except Exception as exc:
        print("HR_QUALITY_SPLIT_EXPORT_ERROR " + sanitize_sensitive_text(exc), flush=True)

    # Navigation is now shared by both research dashboards and already sits
    # at the top of the HR Lab. Do not wrap/re-register portfolio routes.
    _INSTALLED = True
    return {
        "installed": True, "version": VERSION,
        "parallel": "HR_CHRONOLOGICAL_V2", "matched_same_decisions": True,
        "extra_provider_calls": 0, "betting_authority": False, "tables": tables,
    }
