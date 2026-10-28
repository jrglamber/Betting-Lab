from __future__ import annotations

"""Surface forward CLV-First >0 selections in the lightweight Challenge Export.

Export/presentation only. This wrapper runs after the existing challenge portfolio
refresh, adds no provider calls, and does not change model selection, staking,
alerts, execution, or betting authority.
"""

import csv
import io
import json
import math
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional

import challenge_export_patch as base

VERSION = "CHALLENGE_EXPORT_V4_CLV_SHORTLIST_2026_10_28"
STATUS = "RESEARCH_BACKED_SHORTLIST"
_INSTALLED = False
_ORIGINAL_BUILD = None
_RELEASE_VERSION = None

_SOURCE_META = {
    "execution_shadow_bets": ("CORE", "events"),
    "multisport_execution_bets": ("MULTISPORT_H2H", "multisport_events"),
    "multisport_line_bets": ("MULTISPORT_LINES", "multisport_events"),
    "tennis_execution_bets": ("TENNIS_EXEC", "tennis_events"),
}


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if out.tzinfo is None:
            out = out.replace(tzinfo=timezone.utc)
        return out.astimezone(timezone.utc)
    except Exception:
        return None


def _read_csv(data: bytes) -> list[dict[str, str]]:
    if not data:
        return []
    text = data.decode("utf-8-sig", errors="replace")
    return [dict(row) for row in csv.DictReader(io.StringIO(text))]


def _write_csv(rows: list[Mapping[str, Any]]) -> bytes:
    if not rows:
        return b""
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            key = str(key)
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: "" if row.get(k) is None else row.get(k) for k in fieldnames})
    return out.getvalue().encode("utf-8")


def _safe_fetchall(db, sql: str, params=()) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in db.fetchall(sql, params)]
    except Exception:
        return []


def _safe_fetchone(db, sql: str, params=()) -> dict[str, Any]:
    try:
        row = db.fetchone(sql, params)
        return dict(row) if row else {}
    except Exception:
        return {}


def _forward_gt0_samples(db, now: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = _safe_fetchall(
        db,
        """SELECT *
           FROM clv_first_samples
           WHERE evidence_mode='FORWARD'
             AND status='OPEN'
             AND decision='ACCEPT'
             AND predicted_clv_pct > 0
           ORDER BY commence_time, predicted_clv_pct DESC, id""",
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        commence = _parse_dt(row.get("commence_time"))
        if not commence or not (now < commence <= end):
            continue
        if str(row.get("source_table") or "") not in _SOURCE_META:
            continue
        out.append(row)
    return out


def _event_for_sample(db, sample: Mapping[str, Any]) -> dict[str, Any]:
    source_table = str(sample.get("source_table") or "")
    _, event_table = _SOURCE_META.get(source_table, ("", ""))
    event_id = str(sample.get("event_id") or "")
    if event_table and event_id:
        row = _safe_fetchone(
            db,
            f"SELECT * FROM {event_table} WHERE event_id=?",
            (event_id,),
        )
        if row:
            return row
    return {
        "event_id": sample.get("event_id"),
        "commence_time": sample.get("commence_time"),
        "sport_key": sample.get("sport_key"),
        "league": sample.get("league"),
    }


def _source_bet(db, sample: Mapping[str, Any]) -> dict[str, Any]:
    source_table = str(sample.get("source_table") or "")
    if source_table not in _SOURCE_META:
        return {}
    source_id = sample.get("source_bet_id")
    if source_id is None:
        return {}
    row = _safe_fetchone(db, f"SELECT * FROM {source_table} WHERE id=?", (source_id,))
    if row:
        row["_source_table"] = source_table
    return row


def _candidate_from_sample(db, sample: Mapping[str, Any]) -> dict[str, Any]:
    source_table = str(sample.get("source_table") or "")
    source_name, _ = _SOURCE_META[source_table]
    bet = _source_bet(db, sample)
    event = _event_for_sample(db, sample)

    if bet:
        row = base._normal_candidate(source_name, bet, event)
    else:
        # Defensive fallback: the forward sample itself has all key selection
        # fields even if a legacy source row is later unavailable.
        row = {
            "source": source_name,
            "source_table": source_table,
            "source_id": sample.get("source_bet_id"),
            "event_id": sample.get("event_id"),
            "commence_time": sample.get("commence_time"),
            "sport_key": sample.get("sport_key"),
            "league": sample.get("league"),
            "home_team": event.get("home_team") or event.get("player_one"),
            "away_team": event.get("away_team") or event.get("player_two"),
            "market_key": sample.get("market_key"),
            "selection": sample.get("selection"),
            "point": sample.get("point"),
            "bookmaker_key": sample.get("bookmaker_key"),
            "bookmaker_title": None,
            "offered_odds": sample.get("entry_odds"),
            "odds_band": "",
            "fair_probability": None,
            "fair_odds": None,
            "edge_pct": sample.get("edge_pct"),
            "min_odds": None,
            "status": sample.get("status"),
            "created_at": sample.get("created_at"),
            "notes": sample.get("source_lane"),
        }

    row.update(
        {
            "clv_first_sample_id": sample.get("id"),
            "clv_first_source_lane": sample.get("source_lane"),
            "clv_first_predicted_clv_pct": sample.get("predicted_clv_pct"),
            "clv_first_predicted_positive_prob": sample.get("predicted_positive_prob"),
            "clv_first_confidence": sample.get("confidence"),
            "clv_first_decision": sample.get("decision"),
            "clv_first_threshold_bucket": sample.get("threshold_bucket"),
            "clv_first_model_version": sample.get("model_version"),
            "clv_first_filter": "PASS_GT0_RESEARCH_BACKED_SHORTLIST",
            "clv_first_filter_authority": "MANUAL_REVIEW_ONLY",
            "clv_first_shortlist": "YES",
            "clv_first_shortlist_status": STATUS,
            "clv_first_auto_stake_gbp": 0.0,
            "clv_first_manual_price_check_required": True,
            "clv_first_entry_odds_at_sample": sample.get("entry_odds"),
            "clv_first_sample_created_at": sample.get("created_at"),
            "challenge_lane": "CLV_FIRST_GT0",
            "challenge_status": STATUS,
            "challenge_stake_gbp": 0.0,
            "challenge_note": (
                "Forward predicted-CLV >0 shortlist. Manual price/edge review required; "
                "no automatic challenge staking authority."
            ),
        }
    )
    return row


def _candidate_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("source_table") or ""),
        str(row.get("source_id") or row.get("source_bet_id") or ""),
    )


def _annotate_existing_from_sample(row: dict[str, Any], sample: Mapping[str, Any]) -> None:
    row.update(
        {
            "clv_first_sample_id": sample.get("id"),
            "clv_first_source_lane": sample.get("source_lane"),
            "clv_first_predicted_clv_pct": sample.get("predicted_clv_pct"),
            "clv_first_predicted_positive_prob": sample.get("predicted_positive_prob"),
            "clv_first_confidence": sample.get("confidence"),
            "clv_first_decision": sample.get("decision"),
            "clv_first_threshold_bucket": sample.get("threshold_bucket"),
            "clv_first_model_version": sample.get("model_version"),
            "clv_first_filter": "PASS_GT0_RESEARCH_BACKED_SHORTLIST",
            "clv_first_filter_authority": "MANUAL_REVIEW_ONLY",
            "clv_first_shortlist": "YES",
            "clv_first_shortlist_status": STATUS,
            "clv_first_auto_stake_gbp": 0.0,
            "clv_first_manual_price_check_required": True,
            "clv_first_entry_odds_at_sample": sample.get("entry_odds"),
            "clv_first_sample_created_at": sample.get("created_at"),
        }
    )


def _lane_snapshot(db) -> dict[str, Any]:
    rows = _safe_fetchall(
        db,
        """SELECT predicted_clv_pct,predicted_positive_prob,decision,status,result,
                  pnl_units,actual_clv_pct,clv_quality,created_at,settled_at
           FROM clv_first_samples
           WHERE evidence_mode='FORWARD'
             AND decision='ACCEPT'
             AND predicted_clv_pct > 0""",
    )
    settled = [r for r in rows if _number(r.get("pnl_units")) is not None]
    pnl = sum(_number(r.get("pnl_units")) or 0.0 for r in settled)
    ab = [
        r for r in settled
        if str(r.get("clv_quality") or "").upper() in {"A", "B"}
        and _number(r.get("actual_clv_pct")) is not None
    ]
    clvs = [_number(r.get("actual_clv_pct")) for r in ab]
    clvs = [x for x in clvs if x is not None]
    state = _safe_fetchone(
        db,
        "SELECT state_value FROM clv_first_state WHERE state_key=?",
        ("clv_first_forward_start_at",),
    )
    return {
        "lane": "CLV_FIRST_GT0",
        "status": STATUS,
        "threshold": "predicted_clv_pct > 0",
        "forward_start_at": state.get("state_value"),
        "samples": len(rows),
        "settled": len(settled),
        "pnl_units": pnl,
        "roi_pct": (pnl / len(settled) * 100.0) if settled else None,
        "ab_closes": len(clvs),
        "avg_ab_clv_pct": (sum(clvs) / len(clvs)) if clvs else None,
        "beat_close_pct": (
            sum(1 for x in clvs if x > 0) / len(clvs) * 100.0
            if clvs
            else None
        ),
        "automatic_stake_gbp": 0.0,
        "manual_price_check_required": True,
        "betting_authority": False,
        "note": (
            "Surfaced for challenge decision support because forward >0% evidence is "
            "encouraging. Still research-backed/manual-review only; actual available "
            "price must be checked before any real bet."
        ),
    }


def _upgrade_portfolio_files(files: dict[str, bytes]) -> None:
    rows = _read_csv(files.get("challenge_portfolio.csv", b""))
    changed = False
    for row in rows:
        if str(row.get("lane_key") or "") != "CLV_FIRST_GT0":
            continue
        row["status"] = STATUS
        row["stake_gbp"] = 0.0
        row["rule"] = (
            "Surface every open future CLV-First FORWARD sample with predicted_clv_pct > 0 "
            "for manual challenge review."
        )
        row["rationale"] = (
            "Forward >0% lane has matured enough to inform challenge selection, but it is "
            "not yet promoted to automatic staking authority. Check the current bookmaker "
            "price and the underlying source edge before any bet."
        )
        changed = True
    if changed:
        files["challenge_portfolio.csv"] = _write_csv(rows)

    try:
        summary = json.loads(
            files.get("challenge_portfolio_summary.json", b"{}").decode("utf-8")
        )
    except Exception:
        summary = {}
    if summary:
        research_only = list(summary.get("research_only") or [])
        if "CLV_FIRST_GT0" not in research_only:
            research_only.append("CLV_FIRST_GT0")
        summary["research_only"] = research_only
        rules = dict(summary.get("rules") or {})
        rules["clv_first"] = (
            "predicted CLV >0 is surfaced as RESEARCH_BACKED_SHORTLIST; manual current-price "
            "review is required and automatic challenge stake remains £0"
        )
        summary["rules"] = rules
        summary["clv_first_shortlist_status"] = STATUS
        summary["clv_first_automatic_stake_gbp"] = 0.0
        files["challenge_portfolio_summary.json"] = json.dumps(
            summary, indent=2, ensure_ascii=False
        ).encode("utf-8")


def _rebuild_zip(files: dict[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return payload.getvalue()


def build_challenge_export_v4(db, settings, app_version: str):
    release_version = str(_RELEASE_VERSION or app_version)
    raw_zip, filename = _ORIGINAL_BUILD(db, settings, release_version)

    with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as z:
        files = {name: z.read(name) for name in z.namelist()}

    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=int(getattr(base, "HORIZON_HOURS", 48)))
    samples = _forward_gt0_samples(db, now, end)
    sample_by_key = {
        (str(s.get("source_table") or ""), str(s.get("source_bet_id") or "")): s
        for s in samples
    }

    candidates = _read_csv(files.get("challenge_candidates.csv", b""))
    existing = {_candidate_key(row): row for row in candidates}
    shortlist: list[dict[str, Any]] = []

    for key, sample in sample_by_key.items():
        row = existing.get(key)
        if row is None:
            row = _candidate_from_sample(db, sample)
            candidates.append(row)
            existing[key] = row
        else:
            _annotate_existing_from_sample(row, sample)
        shortlist.append(dict(row))

    candidates.sort(
        key=lambda r: (
            str(r.get("commence_time") or ""),
            str(r.get("source") or ""),
            str(r.get("selection") or ""),
        )
    )
    shortlist.sort(
        key=lambda r: (
            str(r.get("commence_time") or ""),
            -(_number(r.get("clv_first_predicted_clv_pct")) or 0.0),
        )
    )

    snapshot = _lane_snapshot(db)
    _upgrade_portfolio_files(files)
    files["challenge_candidates.csv"] = _write_csv(candidates)
    files["challenge_clv_first_shortlist.csv"] = _write_csv(shortlist)
    files["challenge_clv_first_summary.json"] = json.dumps(
        {
            "version": VERSION,
            "generated_at": now.isoformat(),
            "horizon_hours": int(getattr(base, "HORIZON_HOURS", 48)),
            "open_shortlist_count": len(shortlist),
            "lane_snapshot": snapshot,
            "selection_policy": (
                "Include open future CLV-First FORWARD samples with decision=ACCEPT "
                "and predicted_clv_pct>0. Manual challenge review only."
            ),
            "price_policy": (
                "Do not treat stored entry_odds as a guaranteed current executable price. "
                "Check the user's available bookmaker price before taking a challenge bet."
            ),
            "automatic_staking": False,
            "betting_authority": False,
            "provider_calls_added": 0,
        },
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")

    try:
        summary = json.loads(files.get("challenge_summary.json", b"{}").decode("utf-8"))
    except Exception:
        summary = {}
    summary.update(
        {
            "export_version": VERSION,
            "clv_first_shortlist_enabled": True,
            "clv_first_shortlist_status": STATUS,
            "clv_first_shortlist_count": len(shortlist),
            "clv_first_shortlist_file": "challenge_clv_first_shortlist.csv",
            "clv_first_summary_file": "challenge_clv_first_summary.json",
            "clv_first_snapshot": snapshot,
            "clv_first_auto_stake_gbp": 0.0,
            "clv_first_manual_price_check_required": True,
            "provider_calls_added_by_clv_shortlist": 0,
            "betting_authority": False,
        }
    )
    files["challenge_summary.json"] = json.dumps(
        summary, indent=2, ensure_ascii=False
    ).encode("utf-8")

    try:
        manifest = json.loads(files.get("manifest.json", b"{}").decode("utf-8"))
    except Exception:
        manifest = {}
    manifest["app_version"] = release_version
    manifest["export_version"] = VERSION
    manifest_files = list(manifest.get("files") or [])
    for extra in ("challenge_clv_first_shortlist.csv", "challenge_clv_first_summary.json"):
        if extra not in manifest_files:
            manifest_files.append(extra)
    manifest["files"] = manifest_files
    files["manifest.json"] = json.dumps(
        manifest, indent=2, ensure_ascii=False
    ).encode("utf-8")

    readme = files.get("README.txt", b"").decode("utf-8", errors="replace").rstrip()
    readme += (
        "\n\nCLV-FIRST CHALLENGE SHORTLIST — 28 OCT 2026\n"
        "challenge_clv_first_shortlist.csv contains every still-open future CLV-First "
        "FORWARD sample with predicted_clv_pct > 0 inside the Challenge Export horizon.\n"
        "Passing CLV rows are also present/annotated in challenge_candidates.csv, even "
        "when the source bet was not otherwise included by a promoted challenge lane.\n"
        "challenge_clv_first_summary.json carries the live >0% forward evidence snapshot.\n"
        "This is RESEARCH_BACKED_SHORTLIST only: automatic stake is £0 and the current "
        "available bookmaker price must be checked manually before any challenge bet.\n"
        "No provider calls, selection rules, staking authority or betting authority are added.\n"
    )
    files["README.txt"] = readme.encode("utf-8")

    return _rebuild_zip(files), filename


def install(db, release_version: str) -> Dict[str, Any]:
    global _INSTALLED, _ORIGINAL_BUILD, _RELEASE_VERSION
    _RELEASE_VERSION = str(release_version)
    if _INSTALLED:
        return {
            "installed": True,
            "already_installed": True,
            "version": VERSION,
            "release_version": _RELEASE_VERSION,
        }

    _ORIGINAL_BUILD = base.build_challenge_export
    base.build_challenge_export = build_challenge_export_v4
    base.VERSION = VERSION
    _INSTALLED = True
    return {
        "installed": True,
        "already_installed": False,
        "version": VERSION,
        "release_version": _RELEASE_VERSION,
        "route": base.ROUTE,
        "shortlist_status": STATUS,
        "shortlist_file": "challenge_clv_first_shortlist.csv",
        "summary_file": "challenge_clv_first_summary.json",
        "provider_calls_added": 0,
        "selection_rules_changed": False,
        "staking_authority_changed": False,
        "betting_authority_changed": False,
    }


__all__ = ["VERSION", "STATUS", "install", "build_challenge_export_v4"]
