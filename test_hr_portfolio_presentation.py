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
