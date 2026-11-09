from __future__ import annotations

"""Forward-only, zero-provider-cost HR portfolio shadow.

Uses the canonical evolving HR2 candidate pool (including NCAAF H2H 3-4.99).
No standalone NCAAF feeder, bookmaker execution, extra polling, or alterations
to historical frozen groups. Compares chronological format allocations.
"""

from datetime import datetime, timedelta, timezone
from html import escape
from itertools import combinations
from math import prod
from typing import Any, Mapping
import hashlib
import json

from fastapi.responses import HTMLResponse
import hit_rate_multiples_lab as hr
import hr_market_integrity as integrity
from db import sanitize_sensitive_text, utc_now_iso

VERSION = "HR_PORTFOLIO_FORWARD_2026_11_04_V2"
MANUAL_BOOKS = ("williamhill", "ladbrokes_uk")
TOTAL_STAKE_GBP = 10.0
MAX_GROUPS_PER_DAY = 8
MIN_LEAD_MINUTES = 10
FORMATS = {
    "DOUBLE": ((2,), 2),
    "TRIXIE": ((2, 3), 3),
    "YANKEE": ((2, 3, 4), 4),
}
_INSTALLED = False


def _schema(db):
    id_type = "BIGSERIAL PRIMARY KEY" if db.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute("""CREATE TABLE IF NOT EXISTS hr_portfolio_state (
        id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, policy_version TEXT NOT NULL,
        reference_stake_gbp REAL NOT NULL, rules_json TEXT NOT NULL
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hr_portfolio_groups (
        id {id_type}, group_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
        policy_version TEXT NOT NULL, cohort TEXT NOT NULL, bookmaker_key TEXT NOT NULL,
        selection_count INTEGER NOT NULL, status TEXT NOT NULL,
        reference_stake_gbp REAL NOT NULL, settled_at TEXT
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hr_portfolio_legs (
        id {id_type}, group_id INTEGER NOT NULL, leg_order INTEGER NOT NULL,
        source_lane TEXT NOT NULL, source_table TEXT NOT NULL, source_id INTEGER NOT NULL,
        event_id TEXT NOT NULL, sport_key TEXT, market_key TEXT NOT NULL,
        selection TEXT NOT NULL, point REAL, entry_odds REAL NOT NULL,
        bookmaker_key TEXT NOT NULL, quote_captured_at TEXT NOT NULL,
        home_team TEXT, away_team TEXT, commence_time TEXT,
        result TEXT, settled_at TEXT, UNIQUE(group_id,leg_order)
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS hr_portfolio_formats (
        id {id_type}, group_id INTEGER NOT NULL, format TEXT NOT NULL,
        allocation_pct REAL NOT NULL, status TEXT NOT NULL,
        return_units REAL, pnl_units REAL, settled_at TEXT,
        UNIQUE(group_id,format)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS hr_portfolio_lane_policy (
        source_lane TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'ELIGIBLE',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, review_note TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_hrp_groups_created ON hr_portfolio_groups(created_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_hrp_legs_event ON hr_portfolio_legs(event_id)")


def _state(db):
    row = db.fetchone("SELECT * FROM hr_portfolio_state WHERE id=2")
    if row:
        return dict(row)
    now = utc_now_iso()
    rules = {
        "policy": VERSION, "forward_only": True, "pool": "UNIFIED",
        "ncaaf": "NCAAF H2H 3.00<=p<5.00 enters via standard HR2; no source quotas",
        "lane_governance": "ELIGIBLE enters shadow; WATCH/PAUSED excluded, old evidence retained",
        "strict_same_book_only": list(MANUAL_BOOKS), "quote_freshness_minutes": 15,
        "max_daily_groups": MAX_GROUPS_PER_DAY, "no_source_quotas": True,
        "max_quote_spread_minutes": 5, "one_leg_per_event": True,
        "no_additional_quote_polls": True, "no_bet_authority": True,
        "subsets": "first 2/3/4 qualifying frozen legs, as available",
        "four_leg_portfolio_allocation": {"DOUBLE": 0.5, "TRIXIE": 0.3, "YANKEE": 0.2},
        "three_leg_portfolio_allocation": {"DOUBLE": 0.6, "TRIXIE": 0.4},
        "two_leg_portfolio_allocation": {"DOUBLE": 1.0},
        "singles_benchmark": "all group legs equal weight; zero portfolio allocation",
    }
    db.execute("""INSERT INTO hr_portfolio_state(
        id,started_at,policy_version,reference_stake_gbp,rules_json
    ) VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING""", (
        2, now, VERSION, TOTAL_STAKE_GBP, json.dumps(rules, sort_keys=True),
    ))
    return dict(db.fetchone("SELECT * FROM hr_portfolio_state WHERE id=2"))


def _upcoming(candidate, now):
    kick = hr._parse(candidate.get("commence_time"))
    return bool(kick and now + timedelta(minutes=MIN_LEAD_MINUTES) < kick <=
                now + timedelta(hours=hr.FORMATION_HORIZON_HOURS))


def _base_candidates(db, now):
    policy = (db.fetchone(
        "SELECT policy_version FROM hit_rate_policy_versions WHERE is_active=1 "
        "ORDER BY activated_at DESC LIMIT 1"
    ) or {}).get("policy_version")
    if not policy:
        return []
    rows = db.fetchall("""SELECT * FROM hit_rate_candidates
        WHERE policy_version=? AND experiment_key='HR2' AND
              status IN ('QUEUED','CONSUMED') ORDER BY first_seen_at,id LIMIT 1200""",
        (policy,))
    out = []
    seen = set()
    for raw in rows:
        c = dict(raw)
        if not _upcoming(c, now):
            continue
        key = _leg_identity(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _leg_identity(c):
    return (
        str(c.get("event_id") or "").lower(),
        str(c.get("market_key") or "").lower(),
        str(c.get("selection") or "").strip().lower(),
        str(c.get("point") if c.get("point") is not None else ""),
    )


def _book_options(db, candidates, now, start):
    by_book = {b: [] for b in MANUAL_BOOKS}
    for c in candidates:
        # hr._quotes enforces source/market/point, minimum odds, odds band,
        # quote timestamp and 15-minute freshness. Never use loose tolerance.
        try:
            quoted = hr._quotes(db, c, now, False)
        except Exception:
            continue
        for book in MANUAL_BOOKS:
            q = quoted.get(book)
            if not q:
                continue
            ts = hr._parse(q.get("captured_at"))
            if not ts or ts < start:
                continue
            by_book[book].append((dict(c), dict(q)))
    return by_book


def _pick_unique(items):
    """Chronological baseline: first eligible option from each real fixture.

    A repeated or opposing prediction from the same match cannot count as
    another leg. Totals/spreads without a numerical line are not actionable.
    The full alternative pool remains available to the queue/audit.
    """
    chosen = []
    fixtures = set()
    pool = sorted(items, key=lambda pair: (
        str(pair[0].get("source_created_at") or pair[0].get("first_seen_at") or ""),
        str(pair[0].get("event_id") or ""),
        str(pair[0].get("source_lane") or ""),
    ))
    for item in pool:
        candidate = item[0]
        if not candidate.get("event_id") or not integrity.market_actionable(candidate):
            continue
        fixture = integrity.fixture_key(candidate)
        if fixture in fixtures:
            continue
        chosen.append(item)
        fixtures.add(fixture)
        if len(chosen) == 4:
            break
    return chosen if len(chosen) >= 2 else []


def _eligible_lanes(db, candidates):
    """Freeze first-seen source-lane governance, retain prior review decisions."""
    lanes = sorted({str(c.get("source_lane") or "") for c in candidates if c.get("source_lane")})
    stamp = utc_now_iso()
    for lane in lanes:
        db.execute("""INSERT INTO hr_portfolio_lane_policy(
            source_lane,status,created_at,updated_at,review_note
        ) VALUES(?,?,?,?,?) ON CONFLICT(source_lane) DO NOTHING""", (
            lane, "ELIGIBLE", stamp, stamp,
            "Prospective unified-pool entry; review hit rate, CLV, ROI and sample before promotion",
        ))
    statuses = {
        str(row["source_lane"]): str(row["status"]).upper()
        for row in db.fetchall("SELECT source_lane,status FROM hr_portfolio_lane_policy")
    }
    return {
        lane for lane in lanes if statuses.get(lane) == "ELIGIBLE"
    }


def _allocations(n):
    if n >= 4:
        return {"DOUBLE": 0.5, "TRIXIE": 0.3, "YANKEE": 0.2, "SINGLES": 0.0}
    if n == 3:
        return {"DOUBLE": 0.6, "TRIXIE": 0.4, "SINGLES": 0.0}
    return {"DOUBLE": 1.0, "SINGLES": 0.0}


def _form(db, now, state):
    start = hr._parse(state["started_at"])
    base = _base_candidates(db, now)
    ncaaf = [c for c in base if
             str(c.get("source_lane") or "").startswith("NCAAF_")]
    # All new Portfolio/Quality selections come from the SAME HR2 pool;
    # never inject a parallel 3.00-4.99 NCAAF candidate outside HR2.
    # NCAAF may contribute zero, some or every leg.
    eligible = _eligible_lanes(db, base)
    eligible_candidates = [
        c for c in base
        if str(c.get("source_lane") or "") in eligible
    ]
    # Odds-band qualification can admit both Over and Under 2.5 for the
    # same match. These are NOT two proven edges; abstain from the entire
    # conflicting fixture until a validated directional choice exists.
    conflicting_fixtures = integrity.conflicted_fixture_keys(eligible_candidates)
    unified = [c for c in eligible_candidates
               if integrity.fixture_key(c) not in conflicting_fixtures]
    # Cache the same queue and bookmaker quotes for the two read-only UIs.
    # This must never alter formation or trigger additional provider polling.
    # Shadow observer must never interrupt baseline formation, including
    # minimal test databases without the quote or collector tables.
    try:
        from hr2_relaxed_double_research import observe as observe_relaxed_doubles
        observer_candidates = [
            {**candidate, "fixture_key": str(integrity.fixture_key(candidate))}
            for candidate in unified
        ]
        observe_relaxed_doubles(db, observer_candidates, now)
    except Exception as exc:
        print(f"HR2_RELAXED_DOUBLE_RESEARCH_ERROR {type(exc).__name__}: "
              f"{sanitize_sensitive_text(exc)}", flush=True)
    by_book = _book_options(db, unified, now, start)
    try:
        from hr_portfolio_queues import capture
        capture(db, now, base, eligible, by_book,
                conflicting_fixtures=conflicting_fixtures)
    except Exception as exc:
        print(f"HR_PORTFOLIO_QUEUE_SNAPSHOT_ERROR {type(exc).__name__}: "
              f"{sanitize_sensitive_text(exc)}", flush=True)
    options = []
    for book, entries in by_book.items():
        picks = _pick_unique(entries)
        if picks:
            timestamps = [hr._parse(q.get("captured_at")) for _, q in picks]
            if all(timestamps) and max(timestamps)-min(timestamps) <= timedelta(minutes=5):
                options.append(("UNIFIED", book, picks))
    today = now.date().isoformat() + "T00:00:00+00:00"
    formed = 0
    for cohort, book, picks in options:
        count = (db.fetchone(
            "SELECT COUNT(*) AS n FROM hr_portfolio_groups WHERE created_at>=? AND policy_version=?",
            (today, VERSION),
        ) or {}).get("n") or 0
        if int(count) >= MAX_GROUPS_PER_DAY:
            break
        identities = [_leg_identity(p[0]) for p in picks]
        raw = json.dumps([VERSION, cohort, book, identities], sort_keys=True)
        group_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if db.fetchone("SELECT id FROM hr_portfolio_groups WHERE group_key=?", (group_key,)):
            continue
        stamp = utc_now_iso()
        db.execute("""INSERT INTO hr_portfolio_groups(
            group_key,created_at,policy_version,cohort,bookmaker_key,
            selection_count,status,reference_stake_gbp
        ) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(group_key) DO NOTHING""", (
            group_key, stamp, VERSION, cohort, book, len(picks), "OPEN", TOTAL_STAKE_GBP,
        ))
        g = db.fetchone("SELECT id FROM hr_portfolio_groups WHERE group_key=?", (group_key,))
        if not g:
            continue
        gid = int(g["id"])
        for i, (c, q) in enumerate(picks, 1):
            db.execute("""INSERT INTO hr_portfolio_legs(
                group_id,leg_order,source_lane,source_table,source_id,event_id,
                sport_key,market_key,selection,point,entry_odds,bookmaker_key,
                quote_captured_at,home_team,away_team,commence_time
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(group_id,leg_order) DO NOTHING""", (
                gid, i, c["source_lane"], c["source_table"], int(c["source_id"]),
                c["event_id"], c.get("sport_key"), c["market_key"], c["selection"],
                c.get("point"), float(q["price"]), book, q["captured_at"],
                c.get("home_team"), c.get("away_team"), c.get("commence_time"),
            ))
        for fmt, share in _allocations(len(picks)).items():
            db.execute("""INSERT INTO hr_portfolio_formats(
                group_id,format,allocation_pct,status
            ) VALUES(?,?,?,?) ON CONFLICT(group_id,format) DO NOTHING""",
                (gid, fmt, share, "OPEN"))
        formed += 1
    return {"formed": formed, "hr2_source_candidates": len(base),
            "ncaaf_source_candidates": len(ncaaf), "ncaaf_extra_candidates": 0,
            "eligible_lanes": len(eligible), "unified_candidates": len(unified),
            "conflicting_fixtures_abstained": len(conflicting_fixtures)}


def _return_for(format_name, legs):
    if format_name == "SINGLES":
        orders = ((i,) for i in range(len(legs)))
    else:
        sizes, take = FORMATS[format_name]
        if len(legs) < take:
            return None
        orders = (comb for size in sizes for comb in combinations(range(take), size))
    lines = list(orders)
    total = 0.0
    for combo in lines:
        if any(legs[i]["result"] == "LOSS" for i in combo):
            continue
        total += prod(
            float(legs[i]["entry_odds"]) if legs[i]["result"] == "WIN" else 1.0
            for i in combo
        )
    return total / len(lines) if lines else None


def _settle(db):
    finished = 0
    groups = db.fetchall("SELECT * FROM hr_portfolio_groups WHERE status='OPEN' ORDER BY id")
    for g in groups:
        gid = int(g["id"])
        legs = [dict(x) for x in db.fetchall(
            "SELECT * FROM hr_portfolio_legs WHERE group_id=? ORDER BY leg_order", (gid,)
        )]
        if len(legs) != int(g["selection_count"]):
            continue
        for leg in legs:
            if hr._result(leg.get("result")):
                continue
            result = hr._source_result(db, leg)
            if result:
                db.execute("""UPDATE hr_portfolio_legs SET result=?,settled_at=?
                    WHERE id=?""", (result, utc_now_iso(), int(leg["id"])))
                leg["result"] = result
        if any(hr._result(x.get("result")) is None for x in legs):
            continue
        for leg in legs:
            leg["result"] = hr._result(leg["result"])
        for item in db.fetchall("SELECT * FROM hr_portfolio_formats WHERE group_id=?", (gid,)):
            ret = _return_for(item["format"], legs)
            if ret is None:
                continue
            db.execute("""UPDATE hr_portfolio_formats SET status='SETTLED',
                return_units=?,pnl_units=?,settled_at=? WHERE id=?""", (
                ret, ret-1.0, utc_now_iso(), int(item["id"]),
            ))
        db.execute("UPDATE hr_portfolio_groups SET status='SETTLED',settled_at=? WHERE id=?",
                   (utc_now_iso(), gid))
        finished += 1
    return {"settled_groups": finished}


def status(db):
    _schema(db)
    st = db.fetchone("SELECT * FROM hr_portfolio_state WHERE id=2") or {}
    groups = db.fetchall("""SELECT cohort,status,COUNT(*) AS n FROM hr_portfolio_groups
        WHERE policy_version=? GROUP BY cohort,status ORDER BY cohort,status""", (VERSION,))
    metrics = db.fetchall("""SELECT g.cohort,f.format,COUNT(*) AS settled,
        SUM(f.pnl_units) AS normalized_pnl_units,
        SUM(f.allocation_pct*f.pnl_units) AS portfolio_contribution_units
        FROM hr_portfolio_formats f JOIN hr_portfolio_groups g ON g.id=f.group_id
        WHERE f.status='SETTLED' AND g.policy_version=?
        GROUP BY g.cohort,f.format ORDER BY g.cohort,f.format""", (VERSION,))
    group_pnl = db.fetchall("""SELECT g.id,g.cohort,g.settled_at,
        SUM(f.allocation_pct*f.pnl_units) AS weighted_pnl_units
        FROM hr_portfolio_groups g JOIN hr_portfolio_formats f ON f.group_id=g.id
        WHERE g.status='SETTLED' AND f.status='SETTLED' AND g.policy_version=?
        GROUP BY g.id,g.cohort,g.settled_at ORDER BY g.settled_at,g.id""", (VERSION,))
    portfolio = {}
    for raw in group_pnl:
        cohort = str(raw["cohort"])
        item = portfolio.setdefault(cohort, {
            "cohort": cohort, "settled_groups": 0, "portfolio_pnl_units": 0.0,
            "portfolio_roi_pct": None, "max_drawdown_units": 0.0,
        })
        pnl = float(raw.get("weighted_pnl_units") or 0)
        item["settled_groups"] += 1
        item["portfolio_pnl_units"] += pnl
        # Peak starts at zero; losses cannot masquerade as drawdown recovery.
        running = item["portfolio_pnl_units"]
        peak = item.setdefault("_peak", 0.0)
        peak = max(peak, running)
        item["_peak"] = peak
        item["max_drawdown_units"] = max(item["max_drawdown_units"], peak-running)
    for item in portfolio.values():
        n = item["settled_groups"]
        item["portfolio_roi_pct"] = 100.0 * item["portfolio_pnl_units"] / n if n else None
        item.pop("_peak", None)
    format_rows = []
    for row in metrics:
        item = dict(row)
        n = int(item.get("settled") or 0)
        item["roi_pct"] = 100.0 * float(item.get("normalized_pnl_units") or 0) / n if n else None
        format_rows.append(item)
    latest = db.fetchall("""SELECT g.id,g.created_at,g.cohort,g.bookmaker_key,
        g.status,g.selection_count,
        (SELECT COUNT(*) FROM hr_portfolio_legs l WHERE l.group_id=g.id
         AND l.source_lane LIKE ?) AS ncaaf_legs
        FROM hr_portfolio_groups g WHERE g.policy_version=? ORDER BY g.id DESC LIMIT 25""", ("NCAAF_%", VERSION))
    lane_policy = db.fetchall("""SELECT source_lane,status,review_note,updated_at
        FROM hr_portfolio_lane_policy ORDER BY source_lane""")
    ncaaf_mix = {"zero": 0, "some": 0, "all": 0}
    for g in latest:
        count = int(g.get("ncaaf_legs") or 0)
        total = int(g.get("selection_count") or 0)
        key = "zero" if count == 0 else "all" if count == total else "some"
        ncaaf_mix[key] += 1
    leg_rows = db.fetchall("""SELECT l.source_lane,l.event_id,l.market_key,l.selection,l.point,
        l.result,l.entry_odds
        FROM hr_portfolio_legs l JOIN hr_portfolio_groups g ON g.id=l.group_id
        WHERE g.policy_version=? ORDER BY l.id""", (VERSION,))
    lane_summary = {}
    for raw in leg_rows:
        lane = str(raw.get("source_lane") or "")
        d = lane_summary.setdefault(lane, {"source_lane": lane, "appearances": 0,
                                           "unique_selections": 0, "settled": 0,
                                           "wins": 0, "singles_pnl_units": 0.0})
        d["appearances"] += 1
        identity = _leg_identity(raw)
        seen = d.setdefault("_seen", set())
        if identity in seen:
            continue
        seen.add(identity)
        d["unique_selections"] += 1
        outcome = hr._result(raw.get("result"))
        if outcome is None:
            continue
        d["settled"] += 1
        d["wins"] += int(outcome == "WIN")
        d["singles_pnl_units"] += (
            float(raw.get("entry_odds") or 0)-1 if outcome == "WIN"
            else -1.0 if outcome == "LOSS" else 0.0
        )
    for d in lane_summary.values():
        d.pop("_seen", None)
        d["hit_rate_pct"] = 100*d["wins"]/d["settled"] if d["settled"] else None
        d["singles_roi_pct"] = 100*d["singles_pnl_units"]/d["settled"] if d["settled"] else None
    legacy = db.fetchall("""SELECT policy_version,cohort,COUNT(*) AS groups
        FROM hr_portfolio_groups WHERE policy_version<>?
        GROUP BY policy_version,cohort ORDER BY policy_version,cohort""", (VERSION,))
    return {
        "version": VERSION, "research_only": True, "no_provider_calls": True,
        "pool": "UNIFIED", "no_ncaaf_minimum_or_maximum": True,
        "selection_policy": "FIFO baseline; model-ranked optimiser is not validated",
        "started_at": st.get("started_at"), "bookmakers": list(MANUAL_BOOKS),
        "daily_group_cap": MAX_GROUPS_PER_DAY,
        "groups": [dict(x) for x in groups], "formats": format_rows,
        "portfolio": list(portfolio.values()),
        "lane_policy": [dict(x) for x in lane_policy],
        "lane_metrics": list(lane_summary.values()),
        "recent_ncaaf_mix": ncaaf_mix,
        "archived_v1_groups": [dict(x) for x in legacy],
        "latest_groups": [dict(x) for x in latest],
        "warning": "NCAAF outcome ROI positive but A/B CLV negative; independent forward verification required",
    }


def run_maintenance(db, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    _schema(db)
    st = _state(db)
    formed = _form(db, now, st)
    settled = _settle(db)
    try:
        from hr2_relaxed_double_research import settle as settle_relaxed_doubles
        settle_relaxed_doubles(db, now)
    except Exception as exc:
        print(f"HR2_RELAXED_DOUBLE_SETTLEMENT_ERROR {type(exc).__name__}: "
              f"{sanitize_sensitive_text(exc)}", flush=True)
    payload = {"version": VERSION, **formed, **settled}
    db.record_collector_run("HR_PORTFOLIO_SHADOW_MAINT", True, detail=json.dumps(payload))
    return payload


def install(app, db):
    global _INSTALLED
    if _INSTALLED:
        return {"installed": True, "already_installed": True, "version": VERSION}
    previous = hr.run_maintenance

    def with_portfolio(*args, **kwargs):
        result = previous(*args, **kwargs)
        try:
            run_maintenance(args[0] if args else db, now=kwargs.get("now"))
        except Exception as exc:
            error = sanitize_sensitive_text(exc)
            try:
                db.record_collector_run("HR_PORTFOLIO_SHADOW_MAINT", False, detail=error)
            except Exception:
                pass
            print(f"HR_PORTFOLIO_SHADOW_ERROR {type(exc).__name__}: {error}", flush=True)
        return result

    hr.run_maintenance = with_portfolio

    @app.get("/api/hr-portfolio-shadow/status")
    def portfolio_status():
        return status(db)

    @app.get("/hr-portfolio-shadow", response_class=HTMLResponse)
    def portfolio_page():
        from hr_portfolio_presentation import render_portfolio
        from hr_portfolio_queues import read as read_queue
        return HTMLResponse(render_portfolio(status(db), queue=read_queue(db, view="portfolio")))

    # Keep status/results in both weekly and manual export bundles.
    tables = ("hr2_relaxed_double_observations", "hr_portfolio_state", "hr_portfolio_groups",
              "hr_portfolio_legs", "hr_portfolio_formats", "hr_portfolio_lane_policy")
    try:
        import exporter
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + tables))
    except Exception as exc:
        print(f"HR_PORTFOLIO_FULL_EXPORT_ERROR {type(exc).__name__}: {exc}", flush=True)
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + tables))
        split_exporter.MANUAL_TABLES = tuple(dict.fromkeys(tuple(split_exporter.MANUAL_TABLES) + tables))
    except Exception as exc:
        print(f"HR_PORTFOLIO_SPLIT_EXPORT_ERROR {type(exc).__name__}: {exc}", flush=True)

    # Keep navigation at the top of the existing HR Lab, not below its
    # accumulated cards. Share the same destination with the challenger.
    previous_page = hr._page

    def page_with_link(web_module):
        from hr_portfolio_presentation import main_hr_quicklinks
        return main_hr_quicklinks(previous_page(web_module))

    hr._page = page_with_link
    _INSTALLED = True
    return {"installed": True, "version": VERSION, "forward_only": True,
            "ncaaf_pool": "unified no sport quotas", "extra_paid_api_calls": 0,
            "execution_authority": False, "tables": tables}
