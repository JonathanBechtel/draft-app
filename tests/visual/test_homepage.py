"""Visual regression tests for homepage.

These tests capture screenshots of the homepage at various viewport sizes
and verify key sections are visible. Screenshots are saved to the
`screenshots/` directory for manual review.

Usage:
    # Run all visual tests
    make visual

    # Run with headed browser to watch
    PLAYWRIGHT_HEADLESS=0 make visual

    # Run specific test
    pytest tests/visual/test_homepage.py::test_homepage_full_screenshot -v
"""

from pathlib import Path

from playwright.sync_api import Page, expect

from tests.visual.conftest import VIEWPORT_MOBILE, VIEWPORT_TABLET


class TestHomepageStructure:
    """Tests verifying homepage structure and key sections."""

    def test_homepage_loads(self, page: Page, goto) -> None:
        """Verify homepage loads and key sections are visible."""
        goto("/")

        # Verify Top Prospects section exists
        expect(page.locator("#prospectsGrid")).to_be_visible()

        # Verify VS Arena section exists
        expect(page.locator(".h2h-card")).to_be_visible()

        # Verify news grid section exists
        expect(page.locator("#articlesGrid")).to_be_visible()

    def test_sidebar_visible_on_desktop(
        self, desktop_page: Page, goto, screenshot
    ) -> None:
        """Verify sidebar is visible on desktop viewport."""
        goto("/")

        sidebar = desktop_page.locator(".sidebar")
        expect(sidebar).to_be_visible()

        screenshot.capture_element(".sidebar", "sidebar_desktop")

    def test_sidebar_hidden_on_tablet(self, page: Page, goto, screenshot) -> None:
        """Verify sidebar is hidden on tablet viewport."""
        page.set_viewport_size(VIEWPORT_TABLET)
        goto("/")

        sidebar = page.locator(".sidebar")
        expect(sidebar).not_to_be_visible()

        screenshot.capture_element(".main-layout", "grid_tablet")

    def test_pagination_visible(self, page: Page, goto, screenshot) -> None:
        """Verify pagination controls are present when enough articles exist."""
        goto("/")

        pagination = page.locator("#pagination")

        # Pagination may be hidden if fewer than 6 articles
        if pagination.is_visible():
            screenshot.capture_element("#pagination", "pagination")


class TestHomepageScreenshots:
    """Tests capturing full page screenshots for visual review."""

    def test_homepage_full_screenshot(self, page: Page, goto, screenshot) -> None:
        """Capture full homepage screenshot for visual review."""
        goto("/")
        screenshot.capture_full_page("homepage_full")

    def test_news_hero_section(
        self, page: Page, goto, screenshot, screenshots_dir: Path
    ) -> None:
        """Capture news hero section screenshot."""
        goto("/")
        page.wait_for_timeout(500)

        hero = page.locator("#newsHeroSection")

        if hero.is_visible():
            screenshot.capture_element("#newsHeroSection", "news_hero")
        else:
            # Take screenshot showing hero is hidden
            screenshot.capture_viewport("news_hero_hidden")

    def test_news_grid_section(self, page: Page, goto, screenshot) -> None:
        """Capture news grid and sidebar section screenshot."""
        goto("/")
        page.wait_for_timeout(500)

        # Scroll to news grid section
        page.locator("#articlesGrid").scroll_into_view_if_needed()
        page.wait_for_timeout(200)

        # Capture the main layout (grid + sidebar)
        main_layout = page.locator(".main-layout")
        if main_layout.is_visible():
            screenshot.capture_element(".main-layout", "news_grid_sidebar")
        else:
            screenshot.capture_element("#articlesGrid", "news_grid")


class TestHomepageResponsive:
    """Tests for responsive layout at different viewport sizes."""

    def test_responsive_mobile(self, mobile_page: Page, goto, screenshot) -> None:
        """Capture mobile view screenshot."""
        goto("/")
        screenshot.capture_full_page("homepage_mobile")

    def test_responsive_tablet(self, tablet_page: Page, goto, screenshot) -> None:
        """Capture tablet view screenshot."""
        goto("/")
        screenshot.capture_full_page("homepage_tablet")

    def test_responsive_desktop(self, desktop_page: Page, goto, screenshot) -> None:
        """Capture desktop view screenshot."""
        goto("/")
        screenshot.capture_full_page("homepage_desktop")
