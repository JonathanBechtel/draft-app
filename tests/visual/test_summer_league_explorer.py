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
