from __future__ import annotations

"""CLV-first research lane for Betting Lab.

Research-only and deliberately independent of existing selection engines.
It predicts whether an already-created primary shadow bet will beat the close
using only information available at that bet's creation time.

The model is frozen once at install from clean (A/B) CLV labels that were
already available before the persisted forward-test start. It never gates,
alters, stakes, alerts, or executes an existing bet.
"""

from datetime import datetime, timezone
from html import escape
import json
import math
import threading
import time
from statistics import mean
from typing import Any, Dict

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from db import utc_now_iso

VERSION = "CLV_FIRST_V1"
MODEL_VERSION = "CLV1_FROZEN_HIERARCHICAL_V1"
RUN_EVERY = 600
EXPORT_TABLES = ("clv_first_samples", "clv_first_state")
MIN_TRAINING_SAMPLES = 100
MIN_SEGMENT_SAMPLES = 8
SHRINK_N = 40.0
_STARTED = False
_LOCK = threading.Lock()

SOURCE_SPECS = (
    ("execution_shadow_bets", "events", "FOOTBALL_CORE"),
    ("multisport_execution_bets", "multisport_events", "MULTISPORT_H2H"),
    ("multisport_line_bets", "multisport_events", "MULTISPORT_LINES"),
    ("tennis_execution_bets", "tennis_events", "TENNIS"),
)


def _dt(v):
    try:
        x = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if x.tzinfo is None:
            x = x.replace(tzinfo=timezone.utc)
        return x.astimezone(timezone.utc)
    except Exception:
        return None


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _avg(xs):
    xs = [float(x) for x in xs if _f(x) is not None]
    return mean(xs) if xs else None


def _fmt(v, digits=2):
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return "—"


def _pct(v):
    return "—" if v is None else f"{_fmt(v)}%"


def _tone(v):
    try:
        return "ok" if float(v) >= 0 else "bad"
    except Exception:
        return ""


def _state(db, key):
    row = db.fetchone("SELECT state_value FROM clv_first_state WHERE state_key=?", (key,))
    return row.get("state_value") if row else None


def _set_state(db, key, value):
    db.execute(
        """INSERT INTO clv_first_state(state_key,state_value,updated_at)
           VALUES(?,?,?)
           ON CONFLICT(state_key) DO UPDATE SET
             state_value=excluded.state_value,updated_at=excluded.updated_at""",
        (key, str(value), utc_now_iso()),
    )


def ensure_schema(db):
    ident = "BIGSERIAL PRIMARY KEY" if bool(getattr(db, "is_postgres", False)) else "INTEGER PRIMARY KEY AUTOINCREMENT"
    db.execute(
        """CREATE TABLE IF NOT EXISTS clv_first_state(
             state_key TEXT PRIMARY KEY,
             state_value TEXT NOT NULL,
             updated_at TEXT NOT NULL
           )"""
    )
    db.execute(
        f"""CREATE TABLE IF NOT EXISTS clv_first_samples(
          id {ident},
          sample_key TEXT NOT NULL UNIQUE,
          evidence_mode TEXT NOT NULL,
          created_at TEXT NOT NULL,
          source_table TEXT NOT NULL,
          source_bet_id INTEGER NOT NULL,
          source_lane TEXT NOT NULL,
          event_id TEXT,
          commence_time TEXT,
          sport_key TEXT,
          league TEXT,
          market_key TEXT,
          selection TEXT,
          point REAL,
          bookmaker_key TEXT,
          entry_odds REAL,
          edge_pct REAL,
          hours_to_start REAL,
          odds_band TEXT,
          hours_band TEXT,
          edge_band TEXT,
          predicted_clv_pct REAL,
          predicted_positive_prob REAL,
          confidence TEXT,
          decision TEXT NOT NULL,
          threshold_bucket TEXT,
          features_json TEXT,
          model_version TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'OPEN',
          result TEXT,
          pnl_units REAL,
          actual_clv_pct REAL,
          clv_quality TEXT,
          settled_at TEXT,
          updated_at TEXT NOT NULL
        )"""
    )


def forward_start(db):
    key = "clv_first_forward_start_at"
    value = _state(db, key)
    if not value:
        value = utc_now_iso()
        _set_state(db, key, value)
    return _dt(value) or datetime.now(timezone.utc)


def _event_map(db, table):
    try:
        rows = db.fetchall(f"SELECT * FROM {table}")
    except Exception:
        return {}
    return {str(r.get("event_id") or ""): dict(r) for r in rows if r.get("event_id") is not None}


def _all_source_rows(db):
    out = []
    for table, event_table, lane in SOURCE_SPECS:
        try:
            rows = db.fetchall(f"SELECT * FROM {table} ORDER BY id")
        except Exception:
            continue
        events = _event_map(db, event_table)
        for raw in rows:
            r = dict(raw)
            r["_source_table"] = table
            r["_source_lane"] = lane
            r["_event"] = events.get(str(r.get("event_id") or ""), {})
            out.append(r)
    return out


def _sport_family(row):
    raw = str(row.get("sport_key") or row.get("_event", {}).get("sport_key") or "").lower()
    lane = str(row.get("_source_lane") or "")
    if lane == "TENNIS" or raw.startswith("tennis"):
        return "TENNIS"
    mapping = (
        ("americanfootball_", "AMERICAN_FOOTBALL"),
        ("basketball_", "BASKETBALL"),
        ("baseball_", "BASEBALL"),
        ("icehockey_", "ICE_HOCKEY"),
        ("cricket_", "CRICKET"),
        ("aussierules_", "AUSSIE_RULES"),
        ("rugbyleague_", "RUGBY_LEAGUE"),
        ("soccer_", "FOOTBALL"),
    )
    for prefix, family in mapping:
        if raw.startswith(prefix):
            return family
    if lane == "FOOTBALL_CORE":
        return "FOOTBALL"
    return raw.upper() or lane or "UNKNOWN"


def _league(row):
    e = row.get("_event", {})
    return str(
        row.get("league")
        or row.get("league_title")
        or e.get("league")
        or e.get("league_title")
        or e.get("tournament_title")
        or e.get("tournament")
        or ""
    )


def _commence(row):
    e = row.get("_event", {})
    return row.get("commence_time") or e.get("commence_time")


def _odds_band(odds):
    o = _f(odds)
    if o is None:
        return "UNKNOWN"
    if o < 2.0:
        return "LT2"
    if o < 3.0:
        return "2-2.99"
    if o < 4.0:
        return "3-3.99"
    if o < 5.0:
        return "4-4.99"
    if o < 7.5:
        return "5-7.49"
    return "7.5+"


def _hours_band(hours):
    h = _f(hours)
    if h is None:
        return "UNKNOWN"
    if h < 3:
        return "0-3H"
    if h < 12:
        return "3-12H"
    if h < 24:
        return "12-24H"
    if h < 48:
        return "24-48H"
    return "48H+"


def _edge_band(edge):
    e = _f(edge)
    if e is None:
        return "UNKNOWN"
    if e < 3:
        return "LT3"
    if e < 5:
        return "3-4.99"
    if e < 10:
        return "5-9.99"
    return "10+"


def _features(row):
    created = _dt(row.get("created_at"))
    commence = _dt(_commence(row))
    hours = None
    if created and commence:
        hours = (commence - created).total_seconds() / 3600.0
    odds = _f(row.get("offered_odds") or row.get("entry_odds"))
    edge = _f(row.get("edge_pct"))
    sport = _sport_family(row)
    market = str(row.get("market_key") or "h2h")
    book = str(row.get("bookmaker_key") or "")
    lane = str(row.get("_source_lane") or "")
    ob = _odds_band(odds)
    hb = _hours_band(hours)
    eb = _edge_band(edge)
    return {
        "source_lane": lane,
        "sport": sport,
        "market": market,
        "bookmaker": book,
        "odds_band": ob,
        "hours_band": hb,
        "edge_band": eb,
        "sport_market": f"{sport}|{market}",
        "market_odds": f"{market}|{ob}",
        "sport_odds": f"{sport}|{ob}",
        "book_odds": f"{book}|{ob}" if book else "",
        "source_odds": f"{lane}|{ob}",
        "hours_to_start": hours,
        "entry_odds": odds,
        "edge_pct": edge,
    }


SEGMENT_KEYS = (
    "source_lane",
    "sport",
    "market",
    "bookmaker",
    "odds_band",
    "hours_band",
    "edge_band",
    "sport_market",
    "market_odds",
    "sport_odds",
    "book_odds",
    "source_odds",
)


def _clean_label(row):
    q = str(row.get("clv_quality") or row.get("closing_reference_quality") or "")
    c = _f(row.get("clv_pct"))
    if c is None and q in {"A", "B"}:
        c = _f(row.get("clv_vs_consensus_pct"))
    return c if q in {"A", "B"} else None


def _source_pnl(row):
    for key in ("net_pnl_units", "pnl_units"):
        v = _f(row.get(key))
        if v is not None:
            return v
    result = str(row.get("result") or "").upper()
    odds = _f(row.get("offered_odds") or row.get("entry_odds"))
    if result == "WIN" and odds is not None:
        return odds - 1.0
    if result == "LOSS":
        return -1.0
    if result in {"VOID", "PUSH"}:
        return 0.0
    return None


def _stats(labels):
    labels = list(labels)
    clvs = [float(x["clv"]) for x in labels]
    pnls = [float(x["pnl"]) for x in labels if _f(x.get("pnl")) is not None]
    return {
        "n": len(clvs),
        "mean_clv": _avg(clvs),
        "positive_rate": (sum(1 for x in clvs if x > 0) / len(clvs)) if clvs else None,
        "settled_n": len(pnls),
        "pnl_units": sum(pnls) if pnls else 0.0,
        "roi_pct": (sum(pnls) / len(pnls) * 100.0) if pnls else None,
    }


def _training_labels(db, start):
    labels = []
    for row in _all_source_rows(db):
        created = _dt(row.get("created_at"))
        if not created or created >= start:
            continue
        clv = _clean_label(row)
        if clv is None:
            continue
        labels.append({
            "clv": clv,
            "pnl": _source_pnl(row),
            "features": _features(row),
        })
    return labels


def _train_model(db, start):
    labels = _training_labels(db, start)
    global_stats = _stats(labels)
    segments: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for feature in SEGMENT_KEYS:
        grouped: Dict[str, list] = {}
        for x in labels:
            value = str(x["features"].get(feature) or "")
            if value:
                grouped.setdefault(value, []).append(x)
        segments[feature] = {
            value: _stats(items)
            for value, items in grouped.items()
            if len(items) >= MIN_SEGMENT_SAMPLES
        }

    positive = [x for x in labels if float(x["clv"]) > 0]
    nonpositive = [x for x in labels if float(x["clv"]) <= 0]
    model = {
        "model_version": MODEL_VERSION,
        "trained_at": utc_now_iso(),
        "training_cutoff": start.isoformat(),
        "training_samples": len(labels),
        "global": global_stats,
        "segments": segments,
        "historical_oracle": {
            "positive_clv": _stats(positive),
            "nonpositive_clv": _stats(nonpositive),
        },
        "policy": {
            "primary_accept": "predicted_clv_pct > 0",
            "thresholds_pct": [0.0, 1.0, 2.0, 3.0],
            "min_segment_samples": MIN_SEGMENT_SAMPLES,
            "shrink_n": SHRINK_N,
            "no_live_authority": True,
        },
    }
    _set_state(db, "clv_first_model_json", json.dumps(model, sort_keys=True, default=str))
    _set_state(db, "clv_first_model_version", MODEL_VERSION)
    return model


def _model(db, start):
    raw = _state(db, "clv_first_model_json")
    if raw:
        try:
            model = json.loads(raw)
            if str(model.get("model_version")) == MODEL_VERSION:
                return model
        except Exception:
            pass
    return _train_model(db, start)


def _score(model, features):
    base = model.get("global") or {}
    global_mean = _f(base.get("mean_clv")) or 0.0
    global_prob = _f(base.get("positive_rate"))
    if global_prob is None:
        global_prob = 0.5
    contributions = []
    for feature in SEGMENT_KEYS:
        value = str(features.get(feature) or "")
        if not value:
            continue
        stat = ((model.get("segments") or {}).get(feature) or {}).get(value)
        if not stat:
            continue
        n = float(stat.get("n") or 0)
        if n < MIN_SEGMENT_SAMPLES:
            continue
        w = n / (n + SHRINK_N)
        m = _f(stat.get("mean_clv"))
        p = _f(stat.get("positive_rate"))
        if m is not None and p is not None:
            contributions.append((w, m, p, int(n), feature, value))
    if contributions:
        total_w = sum(x[0] for x in contributions)
        pred_clv = global_mean + sum(w * (m - global_mean) for w, m, _, _, _, _ in contributions) / max(1.0, total_w)
        pred_prob = global_prob + sum(w * (p - global_prob) for w, _, p, _, _, _ in contributions) / max(1.0, total_w)
        effective_n = max(x[3] for x in contributions)
    else:
        pred_clv = global_mean
        pred_prob = global_prob
        effective_n = int(base.get("n") or 0)
    pred_prob = max(0.0, min(1.0, pred_prob))
    confidence = "HIGH" if effective_n >= 100 else "MEDIUM" if effective_n >= 30 else "LOW"
    return pred_clv, pred_prob, confidence, contributions


def _threshold_bucket(pred):
    p = _f(pred)
    if p is None or p <= 0:
        return "PRED_CLV<=0"
    if p >= 3:
        return "PRED_CLV>=3"
    if p >= 2:
        return "PRED_CLV_2-2.99"
    if p >= 1:
        return "PRED_CLV_1-1.99"
    return "PRED_CLV_0-0.99"


def _insert_samples(db, start, model):
    written = 0
    for row in _all_source_rows(db):
        source_table = row["_source_table"]
        source_id = row.get("id")
        if source_id is None:
            continue
        key = f"{source_table}:{source_id}"
        if db.fetchone("SELECT id FROM clv_first_samples WHERE sample_key=?", (key,)):
            continue
        created = _dt(row.get("created_at"))
        if not created:
            continue
        features = _features(row)
        pred, prob, confidence, contributions = _score(model, features)
        mode = "FORWARD" if created >= start else "BACKFILL"
        decision = "ACCEPT" if pred > 0 else "OBSERVE"
        commence = _commence(row)
        odds = _f(row.get("offered_odds") or row.get("entry_odds"))
        edge = _f(row.get("edge_pct"))
        db.execute(
            """INSERT INTO clv_first_samples(
              sample_key,evidence_mode,created_at,source_table,source_bet_id,source_lane,
              event_id,commence_time,sport_key,league,market_key,selection,point,bookmaker_key,
              entry_odds,edge_pct,hours_to_start,odds_band,hours_band,edge_band,
              predicted_clv_pct,predicted_positive_prob,confidence,decision,threshold_bucket,
              features_json,model_version,status,result,pnl_units,actual_clv_pct,clv_quality,
              settled_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key, mode, row.get("created_at"), source_table, int(source_id), row["_source_lane"],
                row.get("event_id"), commence, _sport_family(row), _league(row),
                str(row.get("market_key") or "h2h"), str(row.get("selection") or row.get("outcome_name") or ""),
                _f(row.get("point")), str(row.get("bookmaker_key") or ""), odds, edge,
                features.get("hours_to_start"), features["odds_band"], features["hours_band"], features["edge_band"],
                pred, prob, confidence, decision, _threshold_bucket(pred),
                json.dumps({"features": features, "segments": contributions}, sort_keys=True, default=str),
                MODEL_VERSION, "OPEN", None, None, None, None, None, utc_now_iso(),
            ),
        )
        written += 1
    return written


def _sync_outcomes(db):
    updated = 0
    for table, _, _ in SOURCE_SPECS:
        try:
            samples = db.fetchall(
                "SELECT id,source_bet_id FROM clv_first_samples WHERE source_table=? AND (status='OPEN' OR actual_clv_pct IS NULL)",
                (table,),
            )
        except Exception:
            continue
        for sample in samples:
            try:
                row = db.fetchone(f"SELECT * FROM {table} WHERE id=?", (sample["source_bet_id"],))
            except Exception:
                row = None
            if not row:
                continue
            clv = _f(row.get("clv_pct"))
            quality = str(row.get("clv_quality") or row.get("closing_reference_quality") or "")
            result = str(row.get("result") or "")
            pnl = _source_pnl(row)
            source_status = str(row.get("status") or "")
            is_settled = bool(result) or source_status == "SETTLED" or pnl is not None
            status = "SETTLED" if is_settled else "OPEN"
            settled_at = row.get("settled_at") or (utc_now_iso() if is_settled else None)
            db.execute(
                """UPDATE clv_first_samples
                   SET status=?,result=?,pnl_units=?,actual_clv_pct=?,clv_quality=?,
                       settled_at=COALESCE(settled_at,?),updated_at=?
                   WHERE id=?""",
                (status, result or None, pnl, clv, quality or None, settled_at, utc_now_iso(), sample["id"]),
            )
            updated += 1
    return updated


def _subset_metrics(rows):
    rows = list(rows)
    settled = [r for r in rows if _f(r.get("pnl_units")) is not None]
    pnl = sum(float(r["pnl_units"]) for r in settled)
    clean = [
        r for r in rows
        if str(r.get("clv_quality") or "") in {"A", "B"} and _f(r.get("actual_clv_pct")) is not None
    ]
    return {
        "samples": len(rows),
        "settled": len(settled),
        "pnl_units": pnl,
        "roi_pct": (pnl / len(settled) * 100.0) if settled else None,
        "ab_clv_samples": len(clean),
        "avg_ab_clv_pct": _avg([r["actual_clv_pct"] for r in clean]),
        "beat_close_pct": (
            sum(1 for r in clean if float(r["actual_clv_pct"]) > 0) / len(clean) * 100.0
            if clean else None
        ),
    }


def scoreboard(db):
    start = forward_start(db)
    model = _model(db, start)
    rows = db.fetchall(
        "SELECT * FROM clv_first_samples WHERE evidence_mode='FORWARD' ORDER BY id"
    )
    thresholds = {}
    for threshold in (0.0, 1.0, 2.0, 3.0):
        thresholds[f">{int(threshold)}%" if threshold else ">0%"] = _subset_metrics(
            [r for r in rows if (_f(r.get("predicted_clv_pct")) or -999) > threshold]
        )
    all_metrics = _subset_metrics(rows)
    accepted = [r for r in rows if str(r.get("decision")) == "ACCEPT"]
    accepted_metrics = _subset_metrics(accepted)
    clean_scored = [
        r for r in rows
        if str(r.get("clv_quality") or "") in {"A", "B"} and _f(r.get("actual_clv_pct")) is not None
    ]
    mae = _avg([
        abs(float(r["predicted_clv_pct"]) - float(r["actual_clv_pct"]))
        for r in clean_scored if _f(r.get("predicted_clv_pct")) is not None
    ])
    direction_acc = (
        sum(
            1 for r in clean_scored
            if (float(r.get("predicted_clv_pct") or 0) > 0) == (float(r["actual_clv_pct"]) > 0)
        ) / len(clean_scored) * 100.0
        if clean_scored else None
    )
    return {
        "version": VERSION,
        "model_version": MODEL_VERSION,
        "forward_start_at": start.isoformat(),
        "training_samples": int(model.get("training_samples") or 0),
        "training_ready": int(model.get("training_samples") or 0) >= MIN_TRAINING_SAMPLES,
        "historical_oracle": model.get("historical_oracle") or {},
        "forward": all_metrics,
        "primary_predicted_positive": accepted_metrics,
        "thresholds": thresholds,
        "calibration": {
            "clean_forward_labels": len(clean_scored),
            "direction_accuracy_pct": direction_acc,
            "mean_absolute_clv_error_pct": mae,
        },
        "authority": "RESEARCH_ONLY_NO_EXECUTION",
    }


def latest_samples(db, mode="FORWARD", limit=100):
    return db.fetchall(
        """SELECT * FROM clv_first_samples
           WHERE evidence_mode=?
           ORDER BY id DESC LIMIT ?""",
        (str(mode).upper(), int(limit)),
    )


def run(db):
    summary = {"version": VERSION, "ok": False}
    try:
        ensure_schema(db)
        start = forward_start(db)
        model = _model(db, start)
        summary["training_samples"] = int(model.get("training_samples") or 0)
        summary["inserted"] = _insert_samples(db, start, model)
        summary["synced"] = _sync_outcomes(db)
        summary["scoreboard"] = scoreboard(db)
        summary["ok"] = True
        print("CLV_FIRST " + json.dumps(summary, sort_keys=True, default=str), flush=True)
        try:
            db.record_collector_run("CLV_FIRST_MAINT", True, detail=json.dumps(summary, sort_keys=True, default=str))
        except Exception:
            pass
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        print("CLV_FIRST_ERROR " + summary["error"], flush=True)
        try:
            db.record_collector_run("CLV_FIRST_MAINT", False, detail=summary["error"])
        except Exception:
            pass
    return summary


def patch_exports():
    try:
        import exporter
        exporter.EXPORT_TABLES = tuple(dict.fromkeys(tuple(exporter.EXPORT_TABLES) + EXPORT_TABLES))
    except Exception:
        pass
    try:
        import split_exporter
        split_exporter.WEEKLY_TABLES = tuple(dict.fromkeys(tuple(split_exporter.WEEKLY_TABLES) + EXPORT_TABLES))
    except Exception:
        pass


def loop(db):
    time.sleep(8)
    while True:
        run(db)
        time.sleep(RUN_EVERY)


def install(app: FastAPI, db, base_style: str):
    ensure_schema(db)
    start = forward_start(db)
    _model(db, start)
    patch_exports()

    @app.get("/api/clv-first/status")
    def status_api():
        return scoreboard(db)

    @app.get("/api/clv-first/samples")
    def samples_api(
        mode: str = Query("FORWARD"),
        limit: int = Query(100, ge=1, le=1000),
    ):
        return {
            "mode": str(mode).upper(),
            "rows": latest_samples(db, str(mode).upper(), limit),
        }

    @app.get("/clv-first")
    def dashboard():
        s = scoreboard(db)
        hist = s.get("historical_oracle") or {}
        hp = hist.get("positive_clv") or {}
        hn = hist.get("nonpositive_clv") or {}
        p = s["primary_predicted_positive"]
        c = s["calibration"]
        rows = []
        for label, m in s["thresholds"].items():
            rows.append(
                f"<tr><td>{escape(label)}</td><td>{m['samples']}</td><td>{m['settled']}</td>"
                f"<td class='{_tone(m['pnl_units'])}'>{_fmt(m['pnl_units'])}u</td>"
                f"<td>{_pct(m['roi_pct'])}</td><td>{_pct(m['avg_ab_clv_pct'])}</td>"
                f"<td>{_pct(m['beat_close_pct'])}</td></tr>"
            )
        threshold_rows = "".join(rows) or "<tr><td colspan=7>No forward samples yet.</td></tr>"
        return HTMLResponse(
            f"""<!doctype html><html><head><meta charset=utf-8>
            <meta name=viewport content='width=device-width,initial-scale=1'>
            <title>CLV-First Research</title><style>{base_style}
            .table-wrap{{overflow-x:auto}}body{{max-width:1400px;margin:auto}}</style></head><body>
            <a href='/'>← Betting Lab</a>
            <h1>CLV-First Research <span class=pill>SHADOW · INDEPENDENT · NO EXECUTION AUTHORITY</span></h1>
            <p class=muted>Predict the future closing-price move first. Frozen model; only pre-forward A/B labels train it.
            Existing selection engines are observed, never gated or altered.</p>
            <div class=grid>
              <div class=panel><h2>Forward predicted-positive lane</h2>
                <p>Samples <strong>{p['samples']}</strong> · settled {p['settled']} · A/B closes {p['ab_clv_samples']}</p>
                <p>P&L <strong class='{_tone(p['pnl_units'])}'>{_fmt(p['pnl_units'])}u</strong>
                · ROI {_pct(p['roi_pct'])} · realised A/B CLV {_pct(p['avg_ab_clv_pct'])}
                · beat close {_pct(p['beat_close_pct'])}</p>
              </div>
              <div class=panel><h2>Forward calibration</h2>
                <p>Clean labels {c['clean_forward_labels']} · direction accuracy {_pct(c['direction_accuracy_pct'])}</p>
                <p>Mean absolute CLV error {_pct(c['mean_absolute_clv_error_pct'])}</p>
                <p class=muted>Training samples: {s['training_samples']} · model {escape(s['model_version'])}</p>
              </div>
            </div>
            <div class=panel><h2>Frozen threshold shadows</h2>
              <div class=table-wrap><table><thead><tr><th>Predicted CLV gate</th><th>Samples</th><th>Settled</th>
              <th>P&L</th><th>ROI</th><th>A/B CLV</th><th>Beat close</th></tr></thead>
              <tbody>{threshold_rows}</tbody></table></div>
            </div>
            <div class=panel><h2>Historical oracle reference — not forward evidence</h2>
              <p>Actual positive CLV: n={hp.get('n',0)} · ROI {_pct(hp.get('roi_pct'))} · mean CLV {_pct(hp.get('mean_clv'))}</p>
              <p>Actual non-positive CLV: n={hn.get('n',0)} · ROI {_pct(hn.get('roi_pct'))} · mean CLV {_pct(hn.get('mean_clv'))}</p>
              <p class=muted>This section explains why the lane exists; it is never counted as validation.</p>
            </div>
            <p class=muted>Forward start: {escape(str(s['forward_start_at']))}. Included in Full and Weekly research exports.</p>
            </body></html>"""
        )

    global _STARTED
    with _LOCK:
        if not _STARTED:
            _STARTED = True
            threading.Thread(target=loop, args=(db,), daemon=True, name="clv-first-research").start()
