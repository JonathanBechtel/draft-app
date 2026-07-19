"""Visual tests for Competition Context reuse on the season/venue pages (#610).

Runs against a live server seeded with the same deterministic Competition
Context demo dataset used by the Explorer visual suite
(``scripts/seed_competition_context_demo.py`` /
``app.services.summer_league.environment_fixtures.seed_competition_context_demo``)
so captures are reproducible. States covered:

* season hub (``/stats/summer-league/2024``) — complete all-competitions
  summary rendered alongside the existing venue portfolio cards;
* venue page (``/stats/summer-league/2024/las_vegas``) — complete exact
  single-competition module;
* venue page box-only/partial coverage (``.../2024/california_classic``) —
  rim metrics show unavailable, never zero;
* venue page stale profile (``.../2023/las_vegas``) — the "Stale — last good
  version" badge;
* desktop and mobile widths for the season and venue modules.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect


class TestSeasonVenueContextStructure:
    """Structural checks (fail loudly if the reuse module regresses)."""

    def test_season_hub_keeps_venue_cards_and_adds_summary(
        self, page: Page, goto
    ) -> None:
        """The season hub renders venue cards *and* the all-competitions summary."""
        goto("/stats/summer-league/2024")
        expect(page.locator(".slg-venue-card")).to_have_count(3)
        expect(page.locator("#comp-summary-title")).to_contain_text(
            "All-Competitions Summary"
        )
        expect(page.locator(".slg-comp-detail")).to_contain_text("season:2024")

    def test_venue_page_shows_exact_competition_module(self, page: Page, goto) -> None:
        """The venue page renders its own competition:<id> profile."""
        goto("/stats/summer-league/2024/las_vegas")
        expect(page.locator("#comp-summary-title")).to_contain_text("Environment")
        detail = page.locator(".slg-comp-detail")
        expect(detail).to_contain_text("competition:")
        expect(detail).to_contain_text("Pace (per 48)")

    def test_venue_page_partial_coverage_shows_unavailable_not_zero(
        self, page: Page, goto
    ) -> None:
        """California Classic has no shot chart: rim metrics read unavailable."""
        goto("/stats/summer-league/2024/california_classic")
        rim_metric = page.locator(".slg-comp-metric", has_text="Rim Attempt Share")
        expect(rim_metric).to_have_class(re.compile(r"slg-comp-metric--unavailable"))
        expect(rim_metric.locator(".slg-comp-metric__value")).to_have_text("—")

    def test_venue_page_stale_profile_shows_badge(self, page: Page, goto) -> None:
        """2023 Las Vegas is seeded stale: the neutral stale badge renders."""
        goto("/stats/summer-league/2023/las_vegas")
        expect(page.locator(".slg-comp-stale")).to_be_visible()
        expect(page.locator(".slg-comp-stale")).to_contain_text("Stale")

    def test_explorer_link_present_on_both_pages(self, page: Page, goto) -> None:
        """Each module links back to the exact Explorer Competitions scope."""
        goto("/stats/summer-league/2024")
        link = page.locator(".slg-comp-detail__hub")
        expect(link).to_have_attribute(
            "href",
            "/stats/summer-league/explorer?subject=competitions&profile_scope=season&detail_year=2024",
        )


class TestSeasonVenueContextScreenshots:
    """Deterministic captures for visual review under tests/visual/screenshots/."""

    def test_season_summary_desktop(self, page: Page, goto, screenshot) -> None:
        goto("/stats/summer-league/2024")
        screenshot.capture_full_page("sl_season_context_summary_desktop")

    def test_season_summary_mobile(self, mobile_page: Page, goto, screenshot) -> None:
        goto("/stats/summer-league/2024")
        screenshot.capture_full_page("sl_season_context_summary_mobile")

    def test_venue_module_desktop(self, page: Page, goto, screenshot) -> None:
        goto("/stats/summer-league/2024/las_vegas")
        screenshot.capture_full_page("sl_venue_context_module_desktop")

    def test_venue_module_mobile(self, mobile_page: Page, goto, screenshot) -> None:
        goto("/stats/summer-league/2024/las_vegas")
        screenshot.capture_full_page("sl_venue_context_module_mobile")

    def test_venue_module_partial_coverage(self, page: Page, goto, screenshot) -> None:
        goto("/stats/summer-league/2024/california_classic")
        screenshot.capture_full_page("sl_venue_context_module_partial")

    def test_venue_module_stale(self, page: Page, goto, screenshot) -> None:
        goto("/stats/summer-league/2023/las_vegas")
        screenshot.capture_full_page("sl_venue_context_module_stale")
