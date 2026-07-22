"""Visual + browser-interaction tests for the Competition Context Explorer tab.

Runs against a live server whose database is seeded with the deterministic
Competition Context demo dataset (scripts/seed_competition_context_demo.py) so
captures are reproducible, not dependent on ambient data. States covered
(contract §10): season list/detail/trend, competition list/detail/trend,
partial/box-only coverage, stale, empty/invalid, and desktop/mobile.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

EXPLORER = "/stats/summer-league/explorer"
MOBILE = {"width": 375, "height": 812}


class TestCompetitionExplorerStructure:
    """Structural + interaction checks (fail loudly if the tab regresses)."""

    def test_five_tabs_present(self, page: Page, goto) -> None:
        """Five Explorer tabs render and Competitions is the active one."""
        goto(f"{EXPLORER}?subject=competitions")
        tabs = page.locator(".slg-subject-tab")
        expect(tabs).to_have_count(5)
        active = page.locator('.slg-subject-tab[aria-current="page"]')
        expect(active).to_have_text("Competitions")

    def test_scope_toggle_switches_view(self, page: Page, goto) -> None:
        """Switching to individual competitions renders competition rows."""
        goto(f"{EXPLORER}?subject=competitions")
        expect(page.locator(".slg-comp-scopenote")).to_contain_text("Summer League seasons")
        # The radio is visually hidden (the label is the button), so click the
        # label; the change handler navigates to the individual-competition view.
        page.locator(".slg-scope-btn", has_text="Individual competitions").click()
        page.wait_for_load_state("networkidle")
        expect(page.locator(".slg-comp-scopenote")).to_contain_text("Individual competitions")

    def test_open_season_detail_ajax(self, page: Page, goto) -> None:
        """Clicking a season row opens the five-section profile in place."""
        goto(f"{EXPLORER}?subject=competitions")
        page.get_by_role("link", name="Open profile for 2024 Summer League (all competitions)").click()
        detail = page.locator("#comp-detail")
        expect(detail).to_be_visible()
        expect(detail).to_contain_text("How it played")
        expect(detail).to_contain_text("Data confidence")

    def test_trend_metric_switch(self, page: Page, goto) -> None:
        """Changing the trend metric updates the trend caption without a reload."""
        goto(f"{EXPLORER}?subject=competitions&detail_year=2024")
        page.locator("#comp-trend-metric").select_option("offensive_rating")
        page.wait_for_load_state("networkidle")
        expect(page.locator(".slg-comp-trend__cap")).to_contain_text("Offensive Rating")

    def test_js_off_cold_load(self, browser, base_url) -> None:
        """A shared detail URL renders complete with JavaScript disabled."""
        context = browser.new_context(java_script_enabled=False)
        page = context.new_page()
        page.goto(f"{base_url.rstrip('/')}{EXPLORER}?subject=competitions&detail_year=2024")
        expect(page.locator("#comp-detail")).to_be_visible()
        expect(page.locator(".slg-comp-trend__table")).to_be_visible()
        context.close()

    def test_js_off_default_columns_are_curated(self, browser, base_url) -> None:
        """Column density curation is CSS-only: the "full" columns stay hidden
        by default even with JavaScript disabled (#644)."""
        context = browser.new_context(java_script_enabled=False)
        page = context.new_page()
        page.goto(f"{base_url.rstrip('/')}{EXPLORER}?subject=competitions")
        # A core column (Year) is visible; a full-only column (e.g. a landscape
        # spread metric) is present in the DOM but not visible by default.
        expect(page.locator('[data-col-density="core"]').first).to_be_visible()
        expect(page.locator('[data-col-density="full"]').first).to_be_hidden()
        context.close()

    def test_density_toggle_curates_then_expands_columns(self, page: Page, goto) -> None:
        """Default view hides full-only metric columns; the "Show all metrics"
        control reveals them without a page reload, and hides them again on
        re-toggle (#644). The checkbox is accessibly hidden (clip technique,
        like the existing scope-toggle radios) — a real user clicks the
        visible label, which natively toggles the associated checkbox."""
        goto(f"{EXPLORER}?subject=competitions")
        full_cells = page.locator('[data-col-density="full"]')
        expect(full_cells.first).to_be_hidden()
        label = page.locator('label[for="comp-density-all"]')
        label.click()
        expect(full_cells.first).to_be_visible()
        label.click()
        expect(full_cells.first).to_be_hidden()

    def test_density_toggle_survives_sort_swap(self, page: Page, goto) -> None:
        """Expanding to all metrics stays expanded across an AJAX sort swap."""
        goto(f"{EXPLORER}?subject=competitions")
        page.locator('label[for="comp-density-all"]').click()
        expect(page.locator('[data-col-density="full"]').first).to_be_visible()
        page.get_by_role("link", name="Final GP", exact=False).first.click()
        page.wait_for_load_state("networkidle")
        expect(page.locator("#comp-density-all")).to_be_checked()
        expect(page.locator('[data-col-density="full"]').first).to_be_visible()

    def test_density_control_and_scroll_region_are_accessible(self, page: Page, goto) -> None:
        """Native accessible primitives back the density toggle and the
        horizontal-overflow affordance: a real checkbox+label pair (no custom
        ARIA widget needed) and a labeled, keyboard-focusable scroll region."""
        goto(f"{EXPLORER}?subject=competitions")
        checkbox = page.locator("#comp-density-all")
        expect(checkbox).to_have_attribute("type", "checkbox")
        label = page.locator('label[for="comp-density-all"]')
        expect(label).to_be_visible()
        region = page.locator(".slg-comp-tablewrap")
        expect(region).to_have_attribute("role", "region")
        expect(region).to_have_attribute("aria-label", "Competition results table, scrollable horizontally")
        expect(region).to_have_attribute("tabindex", "0")

    def test_scroll_hint_is_visible_text_not_color_only(self, page: Page, goto) -> None:
        """The overflow affordance is legible text, not a color-only cue."""
        goto(f"{EXPLORER}?subject=competitions")
        expect(page.locator(".slg-comp-scrollhint")).to_contain_text("Scroll for more columns")


class TestCompetitionExplorerScreenshots:
    """Deterministic captures for visual review under tests/visual/screenshots/."""

    def test_season_list_desktop(self, page: Page, goto, screenshot) -> None:
        goto(f"{EXPLORER}?subject=competitions")
        screenshot.capture_full_page("sl_competitions_season_list")

    def test_season_detail_trend_desktop(self, page: Page, goto, screenshot) -> None:
        goto(f"{EXPLORER}?subject=competitions&detail_year=2024&trend_metric=pace_per_48")
        screenshot.capture_full_page("sl_competitions_season_detail")

    def test_competition_list_desktop(self, page: Page, goto, screenshot) -> None:
        goto(f"{EXPLORER}?subject=competitions&profile_scope=competition")
        screenshot.capture_full_page("sl_competitions_competition_list")

    def test_competition_detail_partial(self, page: Page, goto, screenshot) -> None:
        """California Classic is box-only: rim metrics show unavailable, not zero."""
        goto(f"{EXPLORER}?subject=competitions&profile_scope=competition")
        page.get_by_role("link", name="Open profile for 2024 California Classic").click()
        page.wait_for_selector("#comp-detail")
        screenshot.capture_full_page("sl_competitions_competition_detail_partial")

    def test_competition_venue_trend(self, page: Page, goto, screenshot) -> None:
        goto(
            f"{EXPLORER}?subject=competitions&profile_scope=competition"
            "&venue=las_vegas&trend_metric=pace_per_48"
        )
        screenshot.capture_full_page("sl_competitions_venue_trend")

    def test_empty_state(self, page: Page, goto, screenshot) -> None:
        goto(f"{EXPLORER}?subject=competitions&year_min=2099")
        screenshot.capture_full_page("sl_competitions_empty")

    def test_season_list_mobile(self, page: Page, goto, screenshot) -> None:
        page.set_viewport_size(MOBILE)
        goto(f"{EXPLORER}?subject=competitions")
        screenshot.capture_full_page("sl_competitions_season_list_mobile")

    def test_season_detail_mobile(self, page: Page, goto, screenshot) -> None:
        page.set_viewport_size(MOBILE)
        goto(f"{EXPLORER}?subject=competitions&detail_year=2024")
        screenshot.capture_full_page("sl_competitions_season_detail_mobile")

    def test_season_list_all_metrics_expanded_desktop(self, page: Page, goto, screenshot) -> None:
        """#644: curated default vs. full metric matrix, side by side with the
        default capture above."""
        goto(f"{EXPLORER}?subject=competitions")
        page.locator('label[for="comp-density-all"]').click()
        # Clicking scrolled the label into view; reset to the top so the
        # full-page capture doesn't stitch the sticky nav mid-scroll.
        page.evaluate("window.scrollTo(0, 0)")
        screenshot.capture_full_page("sl_competitions_season_list_all_metrics")

    def test_season_list_mobile_curated_default(self, page: Page, goto, screenshot) -> None:
        """#644: mobile default view — curated columns, no JS interaction."""
        page.set_viewport_size(MOBILE)
        goto(f"{EXPLORER}?subject=competitions&profile_scope=competition")
        screenshot.capture_full_page("sl_competitions_competition_list_mobile")
