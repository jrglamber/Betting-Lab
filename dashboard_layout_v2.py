from __future__ import annotations

"""Curated Betting Lab homepage.

Presentation only. The existing dashboard remains available at /dashboard-legacy.
No selection, model, staking, cohort, Telegram, provider or execution rules are
changed here.
"""

from datetime import datetime, timedelta, timezone
from html import escape
import inspect
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from fastapi.responses import HTMLResponse

import web as web_module
import clv_first
import live_yankee_feed
import hit_rate_multiples_lab
from pred4_coverage_diagnostic import latest as pred4_coverage_latest

VERSION = "0.19.55"
_CACHE: Dict[str, Any] = {"at": None, "snapshot": None}
CACHE_SECONDS = 60


def _f(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    number = _f(value)
    return "—" if number is None else f"{number:.{digits}f}{suffix}"


def _tone(value: Any) -> str:
    number = _f(value)
    if number is None:
        return ""
    return "good" if number > 0 else "badx" if number < 0 else ""


def _safe_rows(db, table: str) -> List[Dict[str, Any]]:
    try:
        return [dict(r) for r in db.fetchall(f"SELECT * FROM {table}")]
    except Exception:
        return []


def _filtered(rows: Iterable[Mapping[str, Any]], fn: Callable[[Mapping[str, Any]], bool]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        try:
            if fn(row):
                out.append(dict(row))
        except Exception:
            continue
    return out


def _settled_rows(rows: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    out = []
    for row in rows:
        pnl = row.get("net_pnl_units") if row.get("net_pnl_units") is not None else row.get("pnl_units")
        if _f(pnl) is not None:
            out.append(row)
    return out


def _row_pnl(row: Mapping[str, Any]) -> float:
    value = row.get("net_pnl_units") if row.get("net_pnl_units") is not None else row.get("pnl_units")
    return _f(value) or 0.0


def _row_result(row: Mapping[str, Any]) -> str:
    return str(row.get("result") or row.get("bet_result") or row.get("status") or "").upper()


def _row_clv(row: Mapping[str, Any]) -> Optional[float]:
    quality = str(row.get("clv_quality") or row.get("closing_reference_quality") or "").upper()
    if quality not in {"A", "B"}:
        return None
    for key in ("clv_pct", "clv_vs_consensus_pct", "actual_clv_pct", "actual_clv"):
        value = _f(row.get(key))
        if value is not None:
            return value
    return None


def _row_time(row: Mapping[str, Any]) -> datetime:
    for key in ("settled_at", "created_at", "source_created_at", "generated_at"):
        value = row.get(key)
        if not value:
            continue
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _maturity(n: int) -> str:
    if n >= 150:
        return "MATURE"
    if n >= 50:
        return "DEVELOPING"
    return "EARLY"


def _trend(settled: Sequence[Mapping[str, Any]]) -> str:
    if len(settled) < 20:
        return "—"
    ordered = sorted(settled, key=_row_time)
    k = min(25, max(8, len(ordered) // 4))
    recent = ordered[-k:]
    earlier = ordered[:-k]
    if not earlier:
        return "—"
    recent_roi = sum(_row_pnl(r) for r in recent) / len(recent) * 100.0
    earlier_roi = sum(_row_pnl(r) for r in earlier) / len(earlier) * 100.0
    if recent_roi >= earlier_roi + 5.0:
        return "↑"
    if recent_roi <= earlier_roi - 5.0:
        return "↓"
    return "→"


def _lane_stats(name: str, kind: str, rows: Sequence[Mapping[str, Any]], href: str) -> Dict[str, Any]:
    settled = _settled_rows(rows)
    n = len(settled)
    pnl = sum(_row_pnl(r) for r in settled)
    roi = pnl / n * 100.0 if n else None
    wins = sum(1 for r in settled if _row_result(r) == "WIN")
    hit = wins / n * 100.0 if n else None
    clvs = [x for x in (_row_clv(r) for r in rows) if x is not None]
    clv = sum(clvs) / len(clvs) if clvs else None
    maturity = _maturity(n)
    if n < 30:
        evidence = "TOO EARLY"
    elif roi is not None and roi > 0 and (clv is None or clv >= 0):
        evidence = "PROMISING"
    elif roi is not None and roi < 0 and clv is not None and clv < 0:
        evidence = "WEAK"
    else:
        evidence = "MIXED"
    evidence_score = min(n, 150) / 75.0
    if roi is not None:
        evidence_score += 1.0 if roi > 0 else -1.0
    if clv is not None:
        evidence_score += 1.0 if clv > 0 else -1.0
    return {
        "name": name, "kind": kind, "href": href, "settled": n, "pnl": pnl,
        "roi": roi, "hit": hit, "clv": clv, "clv_n": len(clvs),
        "maturity": maturity, "evidence": evidence, "trend": _trend(settled),
        "score": evidence_score,
    }


def _combined(*groups: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for group in groups:
        out.extend(dict(r) for r in group)
    return out


def _stale_count(db) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    specs = (
        ("execution_shadow_bets", "events"),
        ("football_predictive_bets", "events"),
        ("football_predictive_market_bets", "events"),
        ("football_predictive2_bets", "events"),
        ("football_predictive2_market_bets", "events"),
        ("football_predictive3_bets", "events"),
        ("football_predictive3_market_bets", "events"),
        ("football_predictive4_bets", "events"),
        ("football_predictive4_market_bets", "events"),
        ("tennis_execution_bets", "tennis_events"),
        ("multisport_execution_bets", "multisport_events"),
        ("multisport_line_bets", "multisport_events"),
    )
    total = 0
    for table, events in specs:
        try:
            row = db.fetchone(
                f"SELECT COUNT(*) AS n FROM {table} b JOIN {events} e ON e.event_id=b.event_id "
                "WHERE b.status='OPEN' AND e.commence_time<?",
                (cutoff,),
            ) or {}
            total += int(row.get("n") or 0)
        except Exception:
            pass
    return total


def _latest_age_minutes(db, tables: Sequence[str]) -> Optional[float]:
    latest: Optional[datetime] = None
    for table in tables:
        try:
            row = db.fetchone(f"SELECT MAX(captured_at) AS at FROM {table}") or {}
            raw = row.get("at")
            if not raw:
                continue
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            latest = dt if latest is None or dt > latest else latest
        except Exception:
            continue
    if latest is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - latest).total_seconds() / 60.0)


def _pocket_stats(db) -> List[Dict[str, Any]]:
    out = []
    football = _safe_rows(db, "execution_shadow_bets")
    fb = _filtered(football, lambda r: 4.0 <= float(r.get("offered_odds") or 0) < 5.0)
    out.append(_lane_stats("Football 4.00–4.99", "Odds pocket", fb, "/outcome-edge"))
    try:
        multi = [dict(r) for r in db.fetchall(
            "SELECT b.*,e.sport_key,e.league_title FROM multisport_execution_bets b "
            "JOIN multisport_events e ON e.event_id=b.event_id"
        )]
    except Exception:
        multi = []
    pockets = (
        ("NFL 2.00–4.99", lambda r: "nfl" in str(r.get("sport_key") or "").lower() and 2.0 <= float(r.get("offered_odds") or 0) < 5.0),
        ("NCAAF 3.00–4.99", lambda r: ("ncaaf" in str(r.get("sport_key") or "").lower() or "ncaa" in str(r.get("sport_key") or "").lower()) and 3.0 <= float(r.get("offered_odds") or 0) < 5.0),
        ("EuroLeague 1.50–2.99", lambda r: "euroleague" in str(r.get("sport_key") or "").lower() and 1.5 <= float(r.get("offered_odds") or 0) < 3.0),
        ("MLB 1.50–1.99", lambda r: "baseball_mlb" == str(r.get("sport_key") or "").lower() and 1.5 <= float(r.get("offered_odds") or 0) < 2.0),
    )
    for label, predicate in pockets:
        out.append(_lane_stats(label, "Odds pocket", _filtered(multi, predicate), "/multisport"))
    return out


def _snapshot(db) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    cached_at = _CACHE.get("at")
    if isinstance(cached_at, datetime) and (now - cached_at).total_seconds() < CACHE_SECONDS and _CACHE.get("snapshot"):
        return _CACHE["snapshot"]

    tables = {name: _safe_rows(db, name) for name in (
        "execution_shadow_bets", "tennis_execution_bets", "multisport_execution_bets", "multisport_line_bets",
        "football_predictive_bets", "football_predictive_market_bets",
        "football_predictive2_bets", "football_predictive2_market_bets",
        "football_predictive3_bets", "football_predictive3_market_bets",
        "football_predictive4_bets", "football_predictive4_market_bets",
        "football_predictive_research_samples", "api_sports_pred_shadow_bets",
    )}

    forward_research = tables["football_predictive_research_samples"]
    p5 = _filtered(forward_research, lambda r: str(r.get("model_name") or "").upper() == "PRED5" and str(r.get("evidence_mode") or "").upper() == "FORWARD" and str(r.get("decision") or "").upper() == "ACCEPT")
    p6 = _filtered(forward_research, lambda r: str(r.get("model_name") or "").upper() == "PRED6" and str(r.get("evidence_mode") or "").upper() == "FORWARD" and str(r.get("decision") or "").upper() == "ACCEPT")
    api_rows = tables["api_sports_pred_shadow_bets"]

    lanes = [
        _lane_stats("Core Football", "Singles", tables["execution_shadow_bets"], "/outcome-edge"),
        _lane_stats("Tennis", "Singles", tables["tennis_execution_bets"], "/tennis"),
        _lane_stats("Multi-Sport H2H", "Singles", tables["multisport_execution_bets"], "/multisport"),
        _lane_stats("Multi-Sport Lines", "Singles", tables["multisport_line_bets"], "/multisport"),
        _lane_stats("PRED1", "Predictive", _combined(tables["football_predictive_bets"], tables["football_predictive_market_bets"]), "/predictive-football"),
        _lane_stats("PRED2", "Predictive", _combined(tables["football_predictive2_bets"], tables["football_predictive2_market_bets"]), "/predictive-football-pred2"),
        _lane_stats("PRED3", "Predictive", _combined(tables["football_predictive3_bets"], tables["football_predictive3_market_bets"]), "/predictive-football-pred3"),
        _lane_stats("PRED4", "Predictive", _combined(tables["football_predictive4_bets"], tables["football_predictive4_market_bets"]), "/predictive-football-pred4"),
        _lane_stats("PRED5", "Predictive", p5, "/research-progress"),
        _lane_stats("PRED6", "Predictive", p6, "/research-progress"),
        _lane_stats("NFL-PRED1", "Predictive", _filtered(api_rows, lambda r: str(r.get("model_name") or "").upper() == "NFL-PRED1"), "/sport-predictive"),
        _lane_stats("NCAAF-PRED1", "Predictive", _filtered(api_rows, lambda r: str(r.get("model_name") or "").upper() == "NCAAF-PRED1"), "/sport-predictive"),
        _lane_stats("EuroLeague-PRED1", "Predictive", _filtered(api_rows, lambda r: "EUROLEAGUE" in str(r.get("model_name") or "").upper()), "/sport-predictive"),
    ]
    lanes.sort(key=lambda r: (float(r["score"]), int(r["settled"])), reverse=True)

    primary_names = {"Core Football", "Tennis", "Multi-Sport H2H", "Multi-Sport Lines"}
    primary_settled = sum(int(x["settled"]) for x in lanes if x["name"] in primary_names)
    promising = sum(1 for x in lanes if x["evidence"] == "PROMISING")
    stale = _stale_count(db)
    today = web_module.primary_bets_today(db)

    try:
        yankee = dict(live_yankee_feed.scoreboard(db))
    except Exception:
        yankee = {}
    try:
        hit_rate = dict(hit_rate_multiples_lab.scoreboard(db))
    except Exception:
        hit_rate = {}
    try:
        clv = dict(clv_first.scoreboard(db))
    except Exception:
        clv = {}
    try:
        pred4_cov = dict(pred4_coverage_latest(db))
    except Exception:
        pred4_cov = {}

    clv_samples = _safe_rows(db, "clv_first_samples")
    clv_forward = [r for r in clv_samples if str(r.get("evidence_mode") or "").upper() == "FORWARD"]
    clv_positive = sum(1 for r in clv_forward if (_f(r.get("predicted_clv_pct")) or _f(r.get("predicted_clv")) or -999) > 0)

    odds_age = _latest_age_minutes(db, ("odds_snapshots", "tennis_odds_snapshots", "multisport_odds_snapshots"))
    try:
        current_api = db.fetchone("SELECT COUNT(*) AS n FROM api_sports_pred_games WHERE season LIKE '2026%' AND lane_key IN ('NFL','NCAAF')") or {}
        api_current = int(current_api.get("n") or 0)
    except Exception:
        api_current = 0
    tg_failed = 0
    try:
        row = db.fetchone("SELECT COUNT(*) AS n FROM live_yankee_cards WHERE telegram_status='FAILED'") or {}
        tg_failed = int(row.get("n") or 0)
    except Exception:
        pass

    snapshot = {
        "lanes": lanes,
        "pockets": _pocket_stats(db),
        "primary_settled": primary_settled,
        "promising": promising,
        "stale": stale,
        "today": today,
        "yankee": yankee,
        "hit_rate": hit_rate,
        "clv": clv,
        "clv_forward": len(clv_forward),
        "clv_positive": clv_positive,
        "pred4_coverage": pred4_cov,
        "odds_age": odds_age,
        "api_current": api_current,
        "telegram_failed": tg_failed,
    }
    _CACHE["at"] = now
    _CACHE["snapshot"] = snapshot
    return snapshot


def _pill(text: str, css: str = "") -> str:
    return f"<span class='pill {css}'>{escape(text)}</span>"


def _metric(label: str, value: str, note: str = "", css: str = "") -> str:
    return f"<div class='card metric-card'><div class='label'>{escape(label)}</div><div class='value {css}'>{value}</div><div class='muted'>{escape(note)}</div></div>"


def _lane_rows(lanes: Sequence[Mapping[str, Any]]) -> str:
    rows = []
    for lane in lanes:
        ev = str(lane.get("evidence") or "")
        ev_css = "ok" if ev == "PROMISING" else "bad" if ev == "WEAK" else "warn" if ev == "MIXED" else ""
        rows.append(
            f"<tr><td><a href='{escape(str(lane['href']))}'><strong>{escape(str(lane['name']))}</strong></a><div class='muted'>{escape(str(lane['kind']))}</div></td>"
            f"<td>{lane['settled']}</td><td>{_fmt(lane['hit'],1,'%')}</td>"
            f"<td class='{_tone(lane['pnl'])}'>{_fmt(lane['pnl'])}u</td>"
            f"<td class='{_tone(lane['roi'])}'>{_fmt(lane['roi'],1,'%')}</td>"
            f"<td class='{_tone(lane['clv'])}'>{_fmt(lane['clv'],2,'%')}</td>"
            f"<td>{lane['clv_n']}</td><td>{_pill(str(lane['maturity']))}</td>"
            f"<td>{_pill(ev,ev_css)}</td><td class='trend'>{escape(str(lane['trend']))}</td></tr>"
        )
    return "".join(rows)


def _pocket_rows(pockets: Sequence[Mapping[str, Any]]) -> str:
    return "".join(
        f"<tr><td><a href='{escape(str(p['href']))}'><strong>{escape(str(p['name']))}</strong></a></td>"
        f"<td>{p['settled']}</td><td>{_fmt(p['hit'],1,'%')}</td>"
        f"<td class='{_tone(p['pnl'])}'>{_fmt(p['pnl'])}u</td>"
        f"<td class='{_tone(p['roi'])}'>{_fmt(p['roi'],1,'%')}</td>"
        f"<td class='{_tone(p['clv'])}'>{_fmt(p['clv'],2,'%')}</td>"
        f"<td>{_pill(str(p['maturity']))}</td></tr>"
        for p in pockets
    )


def _multiple_card_stats(db, table: str, where: str = "", params: Tuple[Any, ...] = ()) -> Dict[str, Any]:
    try:
        sql = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
        rows = [dict(r) for r in db.fetchall(sql, params)]
    except Exception:
        rows = []
    settled = [r for r in rows if _f(r.get("pnl_units")) is not None]
    pnl = sum(_f(r.get("pnl_units")) or 0.0 for r in settled)
    roi = pnl / len(settled) * 100.0 if settled else None
    return {"cards": len(rows), "settled": len(settled), "pnl": pnl, "roi": roi}


def _page(db, base_style: str) -> str:
    s = _snapshot(db)
    lanes = s["lanes"]
    y = s["yankee"]
    hr = s["hit_rate"]
    hr_streams = hr.get("streams") or {}
    hr1_y = (hr_streams.get("HR1") or {}).get("YANKEE") or {}
    hr1_h = (hr_streams.get("HR1") or {}).get("HEINZ") or {}
    hr2_y = (hr_streams.get("HR2") or {}).get("YANKEE") or {}
    hr2_h = (hr_streams.get("HR2") or {}).get("HEINZ") or {}

    settlement_status = "HEALTHY" if int(s["stale"]) == 0 else "CHECK"
    settlement_css = "ok" if settlement_status == "HEALTHY" else "warn"
    odds_age = s.get("odds_age")
    odds_status = "HEALTHY" if odds_age is not None and odds_age <= 30 else "CHECK"
    odds_css = "ok" if odds_status == "HEALTHY" else "warn"
    pred4_status = str((s.get("pred4_coverage") or {}).get("status") or "WAITING")
    pred4_css = "ok" if pred4_status == "OK" else "warn"
    api_status = "CURRENT" if int(s.get("api_current") or 0) > 0 else "CHECK"
    api_css = "ok" if api_status == "CURRENT" else "warn"
    tg_status = "HEALTHY" if int(s.get("telegram_failed") or 0) == 0 else "CHECK"
    tg_css = "ok" if tg_status == "HEALTHY" else "warn"

    custom_css = """
    body{max-width:1500px;margin:auto}.topnav{position:sticky;top:0;z-index:30;display:flex;gap:8px;flex-wrap:wrap;padding:10px 0 12px;background:var(--bg,#0b1018)}
    .topnav a{padding:8px 12px;border:1px solid #334155;border-radius:999px;text-decoration:none}.hero{padding:8px 0 4px}.hero h1{margin-bottom:4px}.section-title{display:flex;justify-content:space-between;gap:10px;align-items:end;margin:30px 0 10px}.section-title h2{margin:0}.metric-card .value{font-size:30px}.good{color:#22c55e;font-weight:700}.badx{color:#ef4444;font-weight:700}.trend{font-size:22px;font-weight:800}.health-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.health-tile{border:1px solid #334155;border-radius:12px;padding:14px}.health-tile h3{margin:0 0 8px;font-size:15px}.link-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.link-card{display:block;border:1px solid #334155;border-radius:12px;padding:16px;text-decoration:none}.link-card strong{display:block;margin-bottom:5px}.compact-note{font-size:13px}.legacy{margin-top:30px;border-top:1px solid #334155;padding-top:18px}@media(max-width:700px){body{padding:12px}.topnav{position:static}.metric-card .value{font-size:25px}table{min-width:780px}}
    """

    top_metrics = "".join([
        _metric("Collection today", str(s["today"].get("total",0)), "primary selections across all sports"),
        _metric("Primary settled", str(s["primary_settled"]), "core + tennis + multi-sport observations"),
        _metric("Promising lanes", str(s["promising"]), "minimum 30 settled; positive evidence"),
        _metric("Settlement stale", str(s["stale"]), "OPEN >6h after scheduled start", "badx" if s["stale"] else "good"),
        _metric("CLV1 forward", str(s["clv_forward"]), f"predicted-positive {s['clv_positive']}"),
        _metric("Yankee queue", str(y.get("queued",0)), f"cards {y.get('cards',0)} · sent {y.get('telegram_sent',0)}"),
    ])

    predictive_links = "".join([
        "<a class='link-card' href='/predictive-football'><strong>PRED1</strong><span class='muted'>Football Bayesian/Poisson lane</span></a>",
        "<a class='link-card' href='/predictive-football-pred2'><strong>PRED2</strong><span class='muted'>Football Dixon–Coles challenger</span></a>",
        "<a class='link-card' href='/predictive-football-pred3'><strong>PRED3</strong><span class='muted'>Richer football challenger</span></a>",
        "<a class='link-card' href='/predictive-football-pred4'><strong>PRED4</strong><span class='muted'>Current PXG challenger</span></a>",
        "<a class='link-card' href='/research-progress'><strong>PRED5 / PRED6</strong><span class='muted'>Edge Research Lab challengers</span></a>",
        "<a class='link-card' href='/sport-predictive'><strong>NFL / NCAAF / EuroLeague PRED1</strong><span class='muted'>API-Sports predictive models</span></a>",
        "<a class='link-card' href='/clv-first'><strong>CLV1</strong><span class='muted'>Independent closing-price prediction</span></a>",
    ])

    multiples_rows = [
        ("Live Yankee Feed", y.get("cards",0), "—", "—", f"queue {y.get('queued',0)} · price-reactive", "/live-yankee"),
        ("HR1 Yankee", hr1_y.get("cards",0), hr1_y.get("settled",0), _fmt(hr1_y.get("roi_pct"),1,"%"), f"queue {hr1_y.get('queued',0)}", "/hit-rate-multiples"),
        ("HR1 Heinz", hr1_h.get("cards",0), hr1_h.get("settled",0), _fmt(hr1_h.get("roi_pct"),1,"%"), f"queue {hr1_h.get('queued',0)}", "/hit-rate-multiples"),
        ("HR2 Yankee", hr2_y.get("cards",0), hr2_y.get("settled",0), _fmt(hr2_y.get("roi_pct"),1,"%"), f"queue {hr2_y.get('queued',0)}", "/hit-rate-multiples"),
        ("HR2 Heinz", hr2_h.get("cards",0), hr2_h.get("settled",0), _fmt(hr2_h.get("roi_pct"),1,"%"), f"queue {hr2_h.get('queued',0)}", "/hit-rate-multiples"),
    ]
    multiples_html = "".join(
        f"<tr><td><a href='{href}'><strong>{escape(label)}</strong></a></td><td>{cards}</td><td>{settled}</td><td>{roi}</td><td class='muted'>{escape(note)}</td></tr>"
        for label,cards,settled,roi,note,href in multiples_rows
    )

    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Betting Lab v{VERSION}</title><style>{base_style}{custom_css}</style></head><body>
    <div class='hero'><div class='muted'>Project Exit Plan</div><h1>Betting Lab <span class='pill'>v{VERSION}</span></h1>
      <div class='muted'>Forward research overview · shadow only · feature freeze. The homepage prioritises health, evidence and current experiments.</div></div>
    <div class='topnav'>
      <a href='#overview'>Overview</a><a href='#leaderboard'>Research Leaderboard</a><a href='#multiples'>Multiples</a>
      <a href='#predictive'>Predictive Models</a><a href='#pockets'>Odds & Markets</a><a href='#health'>System Health</a>
      <a href='/clv-first'>CLV & Price</a><a href='/export/research-weekly.zip'>Exports</a>
    </div>

    <section id='overview'><div class='section-title'><div><h2>Executive snapshot</h2><div class='muted'>The six things worth checking first.</div></div>{_pill('FEATURE FREEZE','warn')}</div>
      <div class='grid compact'>{top_metrics}</div>
    </section>

    <section id='leaderboard'><div class='section-title'><div><h2>Research Leaderboard</h2><div class='muted'>Evidence order, not ROI order. Sample size, ROI and clean A/B CLV all matter.</div></div><a href='/dashboard-legacy'>Full research detail →</a></div>
      <div class='panel'><div class='table-wrap'><table><thead><tr><th>Lane</th><th>Settled</th><th>Hit rate</th><th>P&L</th><th>ROI</th><th>A/B CLV</th><th>CLV n</th><th>Maturity</th><th>Evidence</th><th>Trend</th></tr></thead><tbody>{_lane_rows(lanes)}</tbody></table></div>
      <div class='muted compact-note' style='margin-top:10px'>EARLY &lt;50 · DEVELOPING 50–149 · MATURE 150+. “Promising” requires at least 30 settled and positive outcome evidence without contradictory clean CLV. This is a research label, not a live-betting promotion.</div></div>
    </section>

    <section id='multiples'><div class='section-title'><div><h2>Multiples Research</h2><div class='muted'>Current assembly experiments in one place.</div></div><div><a href='/live-yankee'>Live Yankee</a> · <a href='/hit-rate-multiples'>Hit-Rate Lab</a></div></div>
      <div class='panel'><div class='table-wrap'><table><thead><tr><th>Study</th><th>Cards</th><th>Settled</th><th>ROI</th><th>State</th></tr></thead><tbody>{multiples_html}</tbody></table></div>
      <div class='nav' style='margin-top:12px'><a href='/manual-systems'>Manual Systems</a><a href='/high-payout-shadow'>High-Payout</a><a href='/cohort-systems'>Cohort Systems</a><a href='/xs1'>Cross-Sport XS</a></div></div>
    </section>

    <section id='predictive'><div class='section-title'><div><h2>Predictive Models</h2><div class='muted'>Model families are grouped here; detailed forecasts remain one click deeper.</div></div></div>
      <div class='link-grid'>{predictive_links}</div>
    </section>

    <section id='pockets'><div class='section-title'><div><h2>Odds & Market Pockets</h2><div class='muted'>Current descriptive pockets worth watching. Still shadow research; none are promoted by this page.</div></div><div><a href='/outcome-edge'>Football</a> · <a href='/multisport'>Multi-Sport</a> · <a href='/tennis'>Tennis</a></div></div>
      <div class='panel'><div class='table-wrap'><table><thead><tr><th>Pocket</th><th>Settled</th><th>Hit rate</th><th>P&L</th><th>ROI</th><th>A/B CLV</th><th>Maturity</th></tr></thead><tbody>{_pocket_rows(s['pockets'])}</tbody></table></div></div>
    </section>

    <section id='health'><div class='section-title'><div><h2>Research Integrity & Health</h2><div class='muted'>Plumbing is condensed here. Anything amber/red deserves investigation before interpreting performance.</div></div></div>
      <div class='health-grid'>
        <div class='health-tile'><h3>Settlement</h3>{_pill(settlement_status,settlement_css)}<div class='muted'>{s['stale']} stale OPEN selections</div></div>
        <div class='health-tile'><h3>Odds freshness</h3>{_pill(odds_status,odds_css)}<div class='muted'>latest snapshot age {_fmt(odds_age,1,'m')}</div></div>
        <div class='health-tile'><h3>PRED4 coverage</h3>{_pill(pred4_status,pred4_css)}<div class='muted'>mechanical gaps {(s.get('pred4_coverage') or {}).get('fit_ok_missing_predictions',0)}</div></div>
        <div class='health-tile'><h3>API-Sports</h3>{_pill(api_status,api_css)}<div class='muted'>{s.get('api_current',0)} current-season NFL/NCAAF games stored</div></div>
        <div class='health-tile'><h3>Telegram</h3>{_pill(tg_status,tg_css)}<div class='muted'>{s.get('telegram_failed',0)} failed Yankee alerts</div></div>
        <div class='health-tile'><h3>CLV-First</h3>{_pill(str(s.get('clv',{}).get('training_ready') and 'COLLECTING' or 'CHECK'),'ok' if s.get('clv',{}).get('training_ready') else 'warn')}<div class='muted'>{s['clv_forward']} forward samples · {s['clv_positive']} predicted-positive</div></div>
      </div>
      <div class='nav' style='margin-top:14px'><a href='/research-progress'>Edge Research diagnostics</a><a href='/clv-first'>CLV ingestion health</a><a href='/dashboard-legacy'>Full legacy dashboard</a><a href='/research'>Research Intelligence</a><a href='/meta-edge'>Meta Edge</a></div>
    </section>

    <section class='legacy'><div class='section-title'><div><h2>Exports & full detail</h2><div class='muted'>Nothing has been removed; the previous dashboard is preserved during the transition.</div></div></div>
      <div class='actions'><a class='button' href='/export/research-weekly.zip'>Weekly Research ZIP</a><a class='button' href='/export/manual-systems.zip'>Manual Systems ZIP</a><a class='button secondary' href='/export/research.zip'>Full Historical ZIP</a><a class='button secondary' href='/dashboard-legacy'>Open full previous dashboard</a></div>
    </section>
    </body></html>"""


def install(app, db, base_style: str) -> Dict[str, Any]:
    root = next((r for r in list(app.router.routes)
                 if getattr(r, "path", None) == "/" and "GET" in (getattr(r, "methods", set()) or set())), None)
    if root is None:
        raise RuntimeError("Betting Lab root route not found")
    if getattr(root.endpoint, "_curated_dashboard_v2", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    legacy_endpoint = root.endpoint
    legacy_name = root.name
    include = getattr(root, "include_in_schema", True)
    app.router.routes.remove(root)

    if not any(getattr(r, "path", None) == "/dashboard-legacy" for r in app.router.routes):
        async def legacy_dashboard():
            response = legacy_endpoint()
            if inspect.isawaitable(response):
                response = await response
            return response
        app.add_api_route("/dashboard-legacy", legacy_dashboard, methods=["GET"], response_class=HTMLResponse,
                          name="dashboard_legacy", include_in_schema=False)

    async def curated_dashboard():
        return HTMLResponse(_page(db, base_style))
    curated_dashboard._curated_dashboard_v2 = True
    app.add_api_route("/", curated_dashboard, methods=["GET"], response_class=HTMLResponse,
                      name=legacy_name, include_in_schema=include)

    web_module.VERSION = VERSION
    return {"installed": True, "already_installed": False, "version": VERSION,
            "legacy_route": "/dashboard-legacy", "presentation_only": True}
