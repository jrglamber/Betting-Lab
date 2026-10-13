from __future__ import annotations

"""Mechanical PRED4 aliases plus small runtime/dashboard installs."""

import web as web_module
from predictive_football_pred4 import ALIAS_NORMALIZATION
from proxy_xg import API_FOOTBALL_LEAGUE_MAP
from multisport_odds_band_patch import install as install_multisport_odds_band_patch
from live_yankee_feed import install as install_live_yankee_feed
from live_yankee_fast_loop import install as install_live_yankee_fast_loop
from live_yankee_consolidation_patch import install as install_live_yankee_consolidation
from betting_lab_research_clarity_patch import install as install_research_clarity
from live_yankee_source_focus_patch import install as install_live_yankee_source_focus
from api_sports_walkforward_dashboard_patch import install as install_walkforward_dashboard
from live_yankee_readiness_patch import install as install_live_yankee_readiness
from betting_lab_shadow_focus_v2_patch import install as install_shadow_focus_v2
from api_sports_paid_dashboard_patch import install as install_paid_sports_dashboard
from live_yankee_expiry_fallback_patch import install as install_yankee_expiry_fallback
from api_sports_paid_status_patch import install as install_paid_sports_status
from live_yankee_results_patch import install as install_yankee_results
from live_yankee_same_book_only_patch import install as install_yankee_same_book_only
from hit_rate_multiples_lab import install as install_hit_rate_multiples
from settlement_integrity_training_fallback_patch import install as install_settlement_integrity
from settlement_lookback_patch import install as install_settlement_lookback

CURRENT_ALIASES = {
    "stade lavallois": "laval",
    "lommel sk": "lommel united",
    "sjk seinajoki": "sjk",
    "west ham united": "west ham",
    "queens park rangers": "qpr",
    # Championship naming used by Odds API vs API-Football.
    "charlton athletic": "charlton",
    "west bromwich albion": "west brom",
    "preston north end": "preston",
}

# Expand PXG1/PRED4 collection only into regular domestic leagues that already
# exist in Betting Lab's Odds API football universe. Multiple aliases are
# intentionally explicit because API-Football league labels can vary by season.
# PRED4's model, minimum-history gates and forecasting rules remain unchanged.
PRED4_DOMESTIC_LEAGUE_EXPANSION = {
    ("argentina", "liga profesional argentina"): "soccer_argentina_primera_division",
    ("argentina", "primera division"): "soccer_argentina_primera_division",
    ("australia", "a league"): "soccer_australia_aleague",
    ("australia", "a league men"): "soccer_australia_aleague",
    ("brazil", "serie a"): "soccer_brazil_campeonato",
    ("brazil", "serie b"): "soccer_brazil_serie_b",
    ("chile", "primera division"): "soccer_chile_campeonato",
    ("china", "super league"): "soccer_china_superleague",
    ("japan", "j1 league"): "soccer_japan_j_league",
    ("japan", "j league"): "soccer_japan_j_league",
    ("south korea", "k league 1"): "soccer_korea_kleague1",
    ("korea republic", "k league 1"): "soccer_korea_kleague1",
    ("mexico", "liga mx"): "soccer_mexico_ligamx",
    ("russia", "premier league"): "soccer_russia_premier_league",
    ("saudi arabia", "pro league"): "soccer_saudi_arabia_pro_league",
    ("saudi arabia", "saudi pro league"): "soccer_saudi_arabia_pro_league",
    ("usa", "major league soccer"): "soccer_usa_mls",
    ("united states", "major league soccer"): "soccer_usa_mls",
    ("usa", "mls"): "soccer_usa_mls",
}


def apply() -> dict:
    before = dict(ALIAS_NORMALIZATION)
    ALIAS_NORMALIZATION.update(CURRENT_ALIASES)

    pxg_before = dict(API_FOOTBALL_LEAGUE_MAP)
    API_FOOTBALL_LEAGUE_MAP.update(PRED4_DOMESTIC_LEAGUE_EXPANSION)
    pxg_changed = sum(
        1
        for key, value in PRED4_DOMESTIC_LEAGUE_EXPANSION.items()
        if pxg_before.get(key) != value
    )
    pxg_new_competitions = sorted(set(PRED4_DOMESTIC_LEAGUE_EXPANSION.values()))

    multisport_dashboard = install_multisport_odds_band_patch()
    live_yankee = install_live_yankee_feed()
    live_yankee_fast_loop = install_live_yankee_fast_loop()
    live_yankee_consolidation = install_live_yankee_consolidation()
    research_clarity = install_research_clarity()
    live_yankee_source_focus = install_live_yankee_source_focus()
    walkforward_dashboard = install_walkforward_dashboard()
    live_yankee_readiness = install_live_yankee_readiness()
    shadow_focus_v2 = install_shadow_focus_v2()
    paid_sports_dashboard = install_paid_sports_dashboard()
    yankee_expiry_fallback = install_yankee_expiry_fallback()
    paid_sports_status = install_paid_sports_status()
    yankee_results = install_yankee_results()
    # Final assembly authority: never consume a cross-book synthetic card.
    # Search the whole queue bookmaker-first, permit only bounded same-book
    # consolidation, and purge/requeue any legacy synthetic card candidates.
    yankee_same_book_only = install_yankee_same_book_only()
    hit_rate_multiples = install_hit_rate_multiples()
    settlement_integrity = install_settlement_integrity()
    settlement_lookback = install_settlement_lookback()
    web_module.VERSION = "0.19.55"
    return {
        "aliases_applied": len(CURRENT_ALIASES),
        "changed": sum(1 for key, value in CURRENT_ALIASES.items() if before.get(key) != value),
        "aliases": dict(CURRENT_ALIASES),
        "pred4_pxg_league_aliases_applied": len(PRED4_DOMESTIC_LEAGUE_EXPANSION),
        "pred4_pxg_league_aliases_changed": pxg_changed,
        "pred4_pxg_new_competitions": pxg_new_competitions,
        "pred4_pxg_new_competition_count": len(pxg_new_competitions),
        "multisport_odds_bands": multisport_dashboard,
        "live_yankee_feed": live_yankee,
        "live_yankee_fast_loop": live_yankee_fast_loop,
        "live_yankee_consolidation": live_yankee_consolidation,
        "research_clarity": research_clarity,
        "live_yankee_source_focus": live_yankee_source_focus,
        "walkforward_dashboard": walkforward_dashboard,
        "live_yankee_readiness": live_yankee_readiness,
        "shadow_focus_v2": shadow_focus_v2,
        "paid_sports_dashboard": paid_sports_dashboard,
        "yankee_expiry_fallback": yankee_expiry_fallback,
        "paid_sports_status": paid_sports_status,
        "yankee_results": yankee_results,
        "yankee_same_book_only": yankee_same_book_only,
        "hit_rate_multiples": hit_rate_multiples,
        "settlement_integrity": settlement_integrity,
        "settlement_lookback": settlement_lookback,
        "dashboard_version": web_module.VERSION,
    }