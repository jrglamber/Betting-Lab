"""Production bootstrap for the adaptive winner/tennis research expansion.

This layers on top of dashboard_app so every existing curated dashboard/runtime
patch remains authoritative. The new research module is shadow-only.
"""

import re

import dashboard_app as curated
import dashboard_responsiveness_patch as dashboard_responsiveness
import adaptive_winner_research as adaptive_core
from adaptive_winner_research import install as install_adaptive_winner_research
from adaptive_winner_research_safety_patch import install as install_adaptive_safety
from adaptive_tpred4_identity_patch import install as install_tpred4_identity
from challenge_clv_shortlist_patch import install as install_challenge_clv_shortlist
from challenge_fhg_health_integrity_patch import install as install_challenge_fhg_health
from challenge_portfolio_refresh_patch import install as install_challenge_portfolio_refresh
from first_half_goal_acca_patch import install as install_fhg_acca
from first_half_goal_free_settlement_patch import install as install_fhg_free_settlement
from first_half_goal_espn_settlement_patch import install as install_fhg_espn_settlement
from first_half_goal_price_capture_patch import install as install_fhg_price_capture
from first_half_goal_price_evidence_patch import install as install_fhg_price_evidence
from first_half_goal_hardening_patch import install as install_fhg_hardening
from first_half_goal_sportsbook_guard_patch import install as install_fhg_sportsbook_guard
from football_coverage_expansion_patch import install as install_football_coverage_expansion
from hit_rate_lane_rotation_patch import install as install_hr_lane_rotation
from homepage_sync_bridge_patch import install as install_homepage_sync_bridge
from live_yankee_runtime_patch import install as install_live_yankee_runtime
from oct25_research_priorities_patch import install as install_research_priorities
from provider_cost_control_patch import install as install_provider_cost_control
from winner_fingerprint_forward_patch import install as install_winner_fingerprint_forward

# v0.19.96 adds the dedicated FHG straight-acca lane and rotates the active HR
# constituent pool while preserving all historical HR evidence and authority.
BUILD_VERSION = "0.19.96"
adaptive_core.DASHBOARD_VERSION = BUILD_VERSION
dashboard_responsiveness.BUILD_VERSION = BUILD_VERSION
curated.dashboard_layout_v2.VERSION = BUILD_VERSION
curated.base_app.web_module.VERSION = BUILD_VERSION
curated.base_app.app.version = BUILD_VERSION

# Install the complete Yankee chain explicitly under the repo-declared Railway
# entrypoint. This removes any dependency on an undocumented service-level start
# command or import side effect. The chain remains manual/shadow only.
try:
    live_yankee_runtime = install_live_yankee_runtime(curated.base_app.db)
except Exception as exc:
    live_yankee_runtime = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"LIVE_YANKEE_RUNTIME_INSTALL {live_yankee_runtime}", flush=True)

# Keep football-data.co.uk as canonical settlement. The compatibility installer
# retains the free-settlement chain and ESPN fallback; the later FHG hardening
# layer disables the persistently 403ing SofaScore attempt and expands ESPN to
# the lower Scottish divisions.
try:
    fhg_free_settlement = install_fhg_free_settlement(curated.base_app.db)
except Exception as exc:
    fhg_free_settlement = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"FHG_FREE_SETTLEMENT_INSTALL {fhg_free_settlement}", flush=True)

try:
    fhg_espn_settlement = install_fhg_espn_settlement(curated.base_app.db)
except Exception as exc:
    fhg_espn_settlement = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"FHG_ESPN_SETTLEMENT_INSTALL {fhg_espn_settlement}", flush=True)

# V2 pricing freezes the first prospective executable Over 0.5 first-half price
# from accessible venues and exports observed price evidence. No price is ever
# backfilled after kickoff.
try:
    fhg_price_capture = install_fhg_price_capture(curated.base_app)
except Exception as exc:
    fhg_price_capture = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"FHG_PRICE_CAPTURE_INSTALL {fhg_price_capture}", flush=True)

try:
    fhg_price_evidence = install_fhg_price_evidence(curated.base_app.db)
except Exception as exc:
    fhg_price_evidence = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"FHG_PRICE_EVIDENCE_INSTALL {fhg_price_evidence}", flush=True)

# Same-book multiple feasibility must mean an accumulator-capable sportsbook,
# not an exchange. Install the guard before the hardening startup pass so even
# its first same-book snapshot excludes Betfair Exchange/Matchbook/Smarkets.
try:
    fhg_sportsbook_guard = install_fhg_sportsbook_guard()
except Exception as exc:
    fhg_sportsbook_guard = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"FHG_SPORTSBOOK_GUARD_INSTALL {fhg_sportsbook_guard}", flush=True)

# Harden the complete FHG research product while leaving FHG1's probability and
# threshold untouched. This also creates FHG2 as a separate prospective-only
# challenger with no selection/betting authority.
try:
    fhg_hardening = install_fhg_hardening(curated.base_app)
except Exception as exc:
    fhg_hardening = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"FHG_HARDENING_INSTALL {fhg_hardening}", flush=True)

# FHG is now a dedicated straight-acca product rather than another systems lane.
# Reuse already-frozen all-book FHG1 quotes to form the largest same-book FULL
# acca plus CORE8 only when >8 compatible legs exist. No extra provider polling;
# the old dedicated 8/9/10 same-book poller is retired after this install.
try:
    fhg_acca = install_fhg_acca(curated.base_app)
except Exception as exc:
    fhg_acca = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"FHG_ACCA_INSTALL {fhg_acca}", flush=True)

# Rotate only future HR constituents under a fresh policy version. Old cards and
# results stay immutable: bench PRED1/2 BTTS No and MLB, tighten EuroLeague core,
# and split NFL into the two evidence bands. Tennis/football-H2H remain probation.
try:
    hr_lane_rotation = install_hr_lane_rotation(curated.base_app.db)
except Exception as exc:
    hr_lane_rotation = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"HR_LANE_ROTATION_INSTALL {hr_lane_rotation}", flush=True)

# Wire the already-written integrity correction into the live challenge export:
# numeric fhg_hit=0 is a real miss, not a blank. Also exposes settlement coverage,
# calibration and tier splits without changing challenge staking authority.
try:
    challenge_fhg_health = install_challenge_fhg_health()
except Exception as exc:
    challenge_fhg_health = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"CHALLENGE_FHG_HEALTH_INSTALL {challenge_fhg_health}", flush=True)

# Broad football coverage must be installed through this exact module because
# Railway launches adaptive_dashboard_app:app. This makes cups/continental and
# additional supported leagues available to the research stack without relying
# on sitecustomize import behaviour. XCOMP1 stays calibration-only/research-only.
try:
    football_coverage_expansion = install_football_coverage_expansion()
except Exception as exc:
    football_coverage_expansion = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(
    f"FOOTBALL_COVERAGE_EXPANSION_ENTRYPOINT {football_coverage_expansion}",
    flush=True,
)

# Install model-freeze and stable historical-identity guards before the runtime
# wrapper is attached. Both are research-only and create no provider traffic.
safety = install_adaptive_safety(curated.base_app.db)
print(f"ADAPTIVE_WINNER_RESEARCH_SAFETY {safety}", flush=True)
tpred4_identity = install_tpred4_identity(curated.base_app.db)
print(f"ADAPTIVE_TPRED4_IDENTITY {tpred4_identity}", flush=True)

adaptive = install_adaptive_winner_research(
    curated.base_app.app,
    curated.base_app.db,
    curated.base_app.BASE_STYLE,
    tennis_engine=getattr(curated.base_app.web_module, "tennis_engine", None),
)
print(f"ADAPTIVE_WINNER_RESEARCH_INSTALL {adaptive}", flush=True)

# Freeze selected discovery fingerprints from the moment the user requested the
# forward test. Membership is prospective and immutable; ROI/CLV determine the
# status. The dashboard presents plain-English scorecards and keeps raw discovery
# details collapsed. No priority or betting rule is changed automatically.
try:
    winner_fingerprint_forward = install_winner_fingerprint_forward(
        curated.base_app.app,
        curated.base_app.db,
    )
except Exception as exc:
    winner_fingerprint_forward = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"WINNER_FINGERPRINT_FORWARD_INSTALL {winner_fingerprint_forward}", flush=True)

# Freeze the newly identified NFL/TS1/broad-PRED4 forward cohorts, expose the
# audit-derived priority/downgrade/conflict states, and export that registry.
# Existing cohorts keep their original frozen_at values; no strategy or staking
# authority is changed. XS2 is deliberately flagged missing rather than guessed.
try:
    research_priorities = install_research_priorities(
        curated.base_app.app,
        curated.base_app.db,
        curated.base_app.BASE_STYLE,
    )
except Exception as exc:
    research_priorities = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"RESEARCH_PRIORITIES_INSTALL {research_priorities}", flush=True)

# The priority-home wrapper is async for historical compatibility, while the
# responsiveness renderer is intentionally synchronous so it runs in FastAPI's
# threadpool. Re-surface the priorities with an equivalent synchronous wrapper
# to prevent asyncio.run() from being invoked inside the ASGI event loop.
try:
    homepage_sync_bridge = install_homepage_sync_bridge(
        curated.base_app.app,
        curated.base_app.db,
    )
except Exception as exc:
    homepage_sync_bridge = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"HOMEPAGE_SYNC_BRIDGE_INSTALL {homepage_sync_bridge}", flush=True)

# Refresh only the lightweight challenge export policy/annotations after the
# 27-Oct audit. The existing /export/challenge.zip route remains in place and
# continues to make zero provider calls. This patch does not change selection,
# staking authority, alerts or execution logic.
try:
    challenge_portfolio_refresh = install_challenge_portfolio_refresh(
        curated.base_app.db,
        BUILD_VERSION,
    )
except Exception as exc:
    challenge_portfolio_refresh = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"CHALLENGE_PORTFOLIO_REFRESH_INSTALL {challenge_portfolio_refresh}", flush=True)

# Surface every still-open forward CLV-First >0 sample inside the challenge
# horizon after the portfolio wrapper is installed. These are manual decision
# candidates only: current bookmaker price must still be checked and the export
# assigns £0 automatic stake/betting authority.
try:
    challenge_clv_shortlist = install_challenge_clv_shortlist(
        curated.base_app.db,
        BUILD_VERSION,
    )
except Exception as exc:
    challenge_clv_shortlist = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"CHALLENGE_CLV_SHORTLIST_INSTALL {challenge_clv_shortlist}", flush=True)

# Install the provider-credit rebalance after the complete runtime stack so its
# ceilings are final. HR Yankee/Heinz remains at 1,000/day; Live Yankee remains
# explicitly preserved at 1,500/day. Lower-priority research funds the increase.
try:
    provider_cost_control = install_provider_cost_control(curated.base_app.db)
except Exception as exc:
    provider_cost_control = {
        "installed": False,
        "error": f"{type(exc).__name__}: {exc}",
    }
print(f"PROVIDER_COST_CONTROL_INSTALL {provider_cost_control}", flush=True)

# The maintenance route is useful during development but deliberately removed
# from the public production surface. Research refreshes run from the wrapped
# tennis cycle instead, so no unauthenticated request can trigger the heavy
# 100k-match initialization/rebuild work.
curated.base_app.app.router.routes[:] = [
    route for route in curated.base_app.app.router.routes
    if getattr(route, "path", None) != "/admin/adaptive-research/run"
]

# Final display guard. The curated homepage has accumulated wrapper layers and
# older bootstrap modules may assign their own VERSION values while importing.
# Wrap the actual page renderer at the end of startup so the rendered title and
# visible badge always match this production release, regardless of import order.
_original_page = curated.dashboard_layout_v2._page


def _page_with_release_version(db, base_style: str) -> str:
    html = _original_page(db, base_style)
    html = re.sub(
        r"(<title>Betting Lab v)[^<]+(</title>)",
        rf"\g<1>{BUILD_VERSION}\g<2>",
        html,
        count=1,
    )
    html = re.sub(
        r"(Betting Lab <span class='pill'>v)[^<]+(</span>)",
        rf"\g<1>{BUILD_VERSION}\g<2>",
        html,
        count=1,
    )
    return html


curated.dashboard_layout_v2._page = _page_with_release_version
adaptive_core.DASHBOARD_VERSION = BUILD_VERSION
dashboard_responsiveness.BUILD_VERSION = BUILD_VERSION
curated.dashboard_layout_v2.VERSION = BUILD_VERSION
curated.base_app.web_module.VERSION = BUILD_VERSION
curated.base_app.app.version = BUILD_VERSION
print(f"BETTING_LAB_RELEASE_VERSION {BUILD_VERSION}", flush=True)

app = curated.base_app.app
