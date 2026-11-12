"""Presentation regressions: preserve the full research lab while cleaning navigation."""
import inspect

import hr_portfolio_presentation as ui
import oct25_research_priorities_patch as priorities


def _portfolio(**overrides):
    base = {
        "started_at": "2026-11-04T10:00:00+00:00",
        "portfolio": [],
        "formats": [],
        "lane_metrics": [],
        "lane_policy": [],
        "latest_groups": [],
        "recent_ncaaf_mix": {"zero": 0, "some": 0, "all": 0},
    }
    return {**base, **overrides}


def _quality(**overrides):
    base = {
        "started_at": "2026-11-04T10:00:00+00:00",
        "paired": 0,
        "pending": 0,
        "baseline_pnl_units": 0,
        "quality_pnl_units": 0,
        "paired_delta_pnl_units": 0,
        "quality_wins": 0,
        "baseline_wins": 0,
        "same_result": 0,
        "formats": [], "latest": [], "skipped": {},
    }
    return {**base, **overrides}


def test_portfolio_uses_existing_dark_theme_and_accessible_responsive_tables():
    html = ui.render_portfolio(_portfolio())
    assert "background:#0d1117" in html
    assert "class='panel'" in html
    assert "class='grid lab-metrics'" in html
    assert "class='lab-table-wrap'" in html
    assert "name='viewport'" in html
    assert "aria-current='page'" in html
    assert "No qualifying same-book groups yet" in html
    assert "border='1'" not in html
    assert "<title>HR Portfolio Shadow | Betting Lab</title>" in html


def test_quality_uses_shared_look_and_top_navigation():
    html = ui.render_quality(_quality())
    assert "background:#0d1117" in html
    assert "class='lab-nav'" in html
    assert "href='/hr-portfolio-shadow'" in html
    assert "href='/hit-rate-multiples'" in html
    assert "No completed paired comparisons yet" in html
    assert "border='1'" not in html


def test_hr_main_links_are_above_the_summary_not_after_all_the_cards():
    old = (
        "<html><head><style>dark</style></head><body>"
        "<h1>Hit-Rate Multiples Lab</h1>"
        "<div class='sub'>Existing explanation</div>"
        "<div class='panel'>Much later data</div>"
        "</body></html>"
    )
    html = ui.main_hr_quicklinks(old)
    assert html.index("href='/hr-portfolio-shadow'") < html.index("Existing explanation")
    assert html.index("href='/hr-quality-challenger'") < html.index("Much later data")
    assert html.count("href='/hr-portfolio-shadow'") == 1
    assert ui.main_hr_quicklinks(html) == html


def test_dashboard_escapes_untrusted_lane_data():
    html = ui.render_portfolio(_portfolio(
        lane_policy=[{"source_lane": "<script>alert(1)</script>",
                      "status": "WATCH", "review_note": "<img src=x onerror=evil()>"}],
    ))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html


def test_portfolio_metrics_show_forward_results_and_source_mix():
    html = ui.render_portfolio(_portfolio(
        portfolio=[{"cohort": "UNIFIED", "settled_groups": 4,
                    "portfolio_pnl_units": 2.8, "portfolio_roi_pct": 70,
                    "max_drawdown_units": 1.4}],
        recent_ncaaf_mix={"zero": 1, "some": 2, "all": 1},
        formats=[{"cohort": "UNIFIED", "format": "DOUBLE", "settled": 4,
                  "normalized_pnl_units": 2.8, "roi_pct": 70}],
    ))
    assert "+2.80u" in html
    assert "+70.0%" in html
    assert "1.40u" in html
    assert "All NCAAF" in html
    assert "Format benchmarks" in html


def test_priority_watch_not_injected_into_homepage_but_audit_preserved():
    code = inspect.getsource(priorities.install)
    assert "home_link = False" in code
    assert "home_link = _surface_home_priorities(" not in code
    assert "/research-priorities" in code
    assert "oe.ensure_watch_cohorts = _ensure_watch" in code
    assert "_patch_exports()" in code


def test_main_hr_comparison_is_mounted_near_top_and_shows_real_matched_data(monkeypatch):
    import hr_rolling_quality_challenger as challenger
    import hr_portfolio_queues as queues
    import hit_rate_multiples_lab as hr

    monkeypatch.setattr(challenger, "scoreboard", lambda db: {
        "baseline_groups_formed": 8, "quality_groups_formed": 6,
        "ranked_displaced_legs": 3, "ranked_changed_groups": 2,
        "matched_doubles": {
            "baseline": {"settled": 4, "hit_rate_pct": 50.0, "roi_pct": -7.5,
                         "max_drawdown_units": 2.5},
            "quality": {"settled": 4, "hit_rate_pct": 75.0, "roi_pct": 18.0,
                        "max_drawdown_units": 1.0},
        },
    })
    monkeypatch.setattr(queues, "read", lambda db, **kwargs: {
        "snapshot_at": "2026-11-12T10:00:00+00:00",
        "competing_fixtures_observed": 5,
    })
    monkeypatch.setattr(hr, "scoreboard", lambda db: {
        "streams": {"HR2": {"DOUBLE": {"cards": 22}}},
    })
    page = (
        "<body><h1>Hit-Rate Multiples Lab</h1><div class='sub'>Intro</div>"
        "<div class='grid'>Key metrics</div>"
        "<div class='section-grid'>Policies</div>"
        "<div class='panel'><h2>Frozen design backtest</h2></div></body>"
    )
    updated = ui.main_hr_comparison_panel(page, object())
    assert updated.index("Key metrics") < updated.index("HR2 strategy comparison")
    assert updated.index("HR2 strategy comparison") < updated.index("Policies")
    assert "FIFO Portfolio Shadow" in updated
    assert "Quality Challenger" in updated
    assert "50.0%" in updated and "75.0%" in updated
    assert "-7.5%" in updated and "18.0%" in updated
    assert "2.50u" in updated and "1.00u" in updated
    assert "Competing fixtures now" in updated and ">5</div>" in updated
    assert "Selections changed" in updated and ">3</div>" in updated
    assert "75.0%" in updated
    assert "standard HR2 Double cards: <strong>22</strong>" in updated
    assert "not these paired portfolio selections" in updated
    assert "id='hr2-strategy-comparison'" in updated
    assert ui.main_hr_comparison_panel(updated, object()) == updated


def test_main_hr_comparison_shows_no_fake_results_when_unsettled(monkeypatch):
    import hr_rolling_quality_challenger as challenger
    import hr_portfolio_queues as queues
    import hit_rate_multiples_lab as hr

    monkeypatch.setattr(challenger, "scoreboard", lambda db: {
        "baseline_groups_formed": 2, "quality_groups_formed": 0,
        "ranked_displaced_legs": 0, "ranked_changed_groups": 0,
        "matched_doubles": {
            "baseline": {"settled": 0, "hit_rate_pct": None, "roi_pct": None},
            "quality": {"settled": 0, "hit_rate_pct": None, "roi_pct": None},
        },
    })
    monkeypatch.setattr(queues, "read", lambda db, **kwargs: {
        "snapshot_at": None,
    })
    monkeypatch.setattr(hr, "scoreboard", lambda db: {
        "streams": {"HR2": {"DOUBLE": {"cards": 0}}},
    })
    panel = ui.main_hr_strategy_comparison(object())
    assert "Awaiting newly settled matched doubles" in panel
    assert "not yet available" in panel
    assert "<td>—</td><td>—</td>" in panel
    assert "0.0%" in panel  # Only comparison formation coverage is genuinely zero.


def test_main_hr_comparison_failure_does_not_break_hr_page(monkeypatch):
    import hr_rolling_quality_challenger as challenger

    def fail(db):
        raise RuntimeError("test unavailable")
    monkeypatch.setattr(challenger, "scoreboard", fail)
    page = "<body><div class='section-grid'>Other HR sections</div></body>"
    updated = ui.main_hr_comparison_panel(page, object())
    assert "Comparison temporarily unavailable" in updated
    assert "Other HR sections" in updated
