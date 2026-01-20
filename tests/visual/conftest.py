"""Pytest fixtures for Playwright visual tests.

This module provides reusable fixtures for screenshot-based visual testing.
Visual tests run against a live server (not ASGI transport) to test the full
rendered output including CSS, JS, and images.

Usage:
    # Run with local dev server (default: http://localhost:8000)
    make visual

    # Run against staging
    TEST_BASE_URL=https://draft-app.fly.dev make visual

    # Run specific test
    pytest tests/visual/test_homepage.py::test_homepage_full_screenshot -v

Environment Variables:
    TEST_BASE_URL: Base URL of the running server (default: http://localhost:8000)
    PLAYWRIGHT_HEADLESS: Set to "0" for headed mode (default: "1" headless)
"""

import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Page

if TYPE_CHECKING:
    from playwright.sync_api import ViewportSize


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _get_base_url() -> str:
    """Return the base URL for visual tests."""
    return os.environ.get("TEST_BASE_URL", "http://localhost:8000")


def _is_headless() -> bool:
    """Return whether to run browser in headless mode."""
    return os.environ.get("PLAYWRIGHT_HEADLESS", "1") != "0"


def _get_screenshots_dir() -> Path:
    """Return the screenshots output directory, creating it if needed."""
    screenshots_dir = Path(__file__).parent / "screenshots"
    screenshots_dir.mkdir(exist_ok=True)
    return screenshots_dir


# ---------------------------------------------------------------------------
# Viewport Presets
# ---------------------------------------------------------------------------

VIEWPORT_DESKTOP: "ViewportSize" = {"width": 1280, "height": 800}
VIEWPORT_TABLET: "ViewportSize" = {"width": 900, "height": 800}
VIEWPORT_MOBILE: "ViewportSize" = {"width": 375, "height": 667}


# ---------------------------------------------------------------------------
# Session-scoped Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def base_url() -> str:
    """Return the base URL for visual tests.

    Returns:
        The base URL from TEST_BASE_URL env var or default localhost.
    """
    return _get_base_url()


@pytest.fixture(scope="session")
def screenshots_dir() -> Path:
    """Return the screenshots output directory.

    Returns:
        Path to the screenshots directory (created if not exists).
    """
    return _get_screenshots_dir()


@pytest.fixture(scope="session")
def browser_type_launch_args() -> dict:
    """Configure browser launch arguments.

    Returns:
        Dict of args passed to browser.launch().
    """
    return {
        "headless": _is_headless(),
    }


@pytest.fixture(scope="session")
def browser_context_args() -> dict:
    """Configure default browser context arguments.

    This sets the default viewport to desktop size. Individual tests can
    override by calling page.set_viewport_size().

    Returns:
        Dict of args passed to browser.new_context().
    """
    return {
        "viewport": VIEWPORT_DESKTOP,
        "device_scale_factor": 1,
    }


# ---------------------------------------------------------------------------
# Function-scoped Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def desktop_page(page: Page) -> Page:
    """Return a page configured with desktop viewport.

    Args:
        page: Playwright page fixture from pytest-playwright.

    Returns:
        The page with desktop viewport (1280x800).
    """
    page.set_viewport_size(VIEWPORT_DESKTOP)
    return page


@pytest.fixture
def tablet_page(page: Page) -> Page:
    """Return a page configured with tablet viewport.

    Args:
        page: Playwright page fixture from pytest-playwright.

    Returns:
        The page with tablet viewport (900x800).
    """
    page.set_viewport_size(VIEWPORT_TABLET)
    return page


@pytest.fixture
def mobile_page(page: Page) -> Page:
    """Return a page configured with mobile viewport.

    Args:
        page: Playwright page fixture from pytest-playwright.

    Returns:
        The page with mobile viewport (375x667).
    """
    page.set_viewport_size(VIEWPORT_MOBILE)
    return page


# ---------------------------------------------------------------------------
# Screenshot Helper Fixtures
# ---------------------------------------------------------------------------

class ScreenshotHelper:
    """Helper class for taking and managing screenshots.

    Provides methods for capturing full page, element, and comparison
    screenshots with consistent naming and directory handling.
    """

    def __init__(self, page: Page, screenshots_dir: Path, base_url: str):
        """Initialize the screenshot helper.

        Args:
            page: Playwright page instance.
            screenshots_dir: Directory to save screenshots.
            base_url: Base URL being tested (for metadata).
        """
        self.page = page
        self.screenshots_dir = screenshots_dir
        self.base_url = base_url

    def capture_full_page(
        self,
        name: str,
        wait_for_idle: bool = True,
        extra_wait_ms: int = 500,
    ) -> Path:
        """Capture a full page screenshot.

        Args:
            name: Base name for the screenshot file (without extension).
            wait_for_idle: Whether to wait for network idle before capture.
            extra_wait_ms: Additional wait time in ms after load state.

        Returns:
            Path to the saved screenshot.
        """
        if wait_for_idle:
            self.page.wait_for_load_state("networkidle")
        if extra_wait_ms > 0:
            self.page.wait_for_timeout(extra_wait_ms)

        path = self.screenshots_dir / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return path

    def capture_viewport(
        self,
        name: str,
        wait_for_idle: bool = True,
        extra_wait_ms: int = 500,
    ) -> Path:
        """Capture a viewport-only screenshot (not full page).

        Args:
            name: Base name for the screenshot file (without extension).
            wait_for_idle: Whether to wait for network idle before capture.
            extra_wait_ms: Additional wait time in ms after load state.

        Returns:
            Path to the saved screenshot.
        """
        if wait_for_idle:
            self.page.wait_for_load_state("networkidle")
        if extra_wait_ms > 0:
            self.page.wait_for_timeout(extra_wait_ms)

        path = self.screenshots_dir / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=False)
        return path

    def capture_element(
        self,
        selector: str,
        name: str,
        scroll_into_view: bool = True,
        wait_for_idle: bool = True,
        extra_wait_ms: int = 200,
    ) -> Path | None:
        """Capture a screenshot of a specific element.

        Args:
            selector: CSS selector for the element.
            name: Base name for the screenshot file (without extension).
            scroll_into_view: Whether to scroll element into view first.
            wait_for_idle: Whether to wait for network idle before capture.
            extra_wait_ms: Additional wait time in ms after scroll.

        Returns:
            Path to the saved screenshot, or None if element not visible.
        """
        if wait_for_idle:
            self.page.wait_for_load_state("networkidle")

        element = self.page.locator(selector)
        if not element.is_visible():
            return None

        if scroll_into_view:
            element.scroll_into_view_if_needed()
            if extra_wait_ms > 0:
                self.page.wait_for_timeout(extra_wait_ms)

        path = self.screenshots_dir / f"{name}.png"
        element.screenshot(path=str(path))
        return path

    def capture_with_timestamp(
        self,
        name: str,
        full_page: bool = True,
    ) -> Path:
        """Capture a screenshot with timestamp in filename.

        Useful for capturing multiple versions during development or
        for archiving test results.

        Args:
            name: Base name for the screenshot file.
            full_page: Whether to capture full page or viewport only.

        Returns:
            Path to the saved screenshot with timestamp.
        """
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(500)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.screenshots_dir / f"{name}_{timestamp}.png"
        self.page.screenshot(path=str(path), full_page=full_page)
        return path


@pytest.fixture
def screenshot(page: Page, screenshots_dir: Path, base_url: str) -> ScreenshotHelper:
    """Provide a ScreenshotHelper instance for the current page.

    Args:
        page: Playwright page fixture.
        screenshots_dir: Directory to save screenshots.
        base_url: Base URL being tested.

    Returns:
        ScreenshotHelper instance configured for this test.
    """
    return ScreenshotHelper(page, screenshots_dir, base_url)


# ---------------------------------------------------------------------------
# Page Navigation Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def goto(page: Page, base_url: str):
    """Return a helper function for navigating to pages.

    Args:
        page: Playwright page fixture.
        base_url: Base URL for the test server.

    Returns:
        A function that navigates to a path and waits for load.
    """
    def _goto(path: str = "/", wait_for_idle: bool = True) -> None:
        """Navigate to a path relative to base_url.

        Args:
            path: URL path to navigate to (default: "/").
            wait_for_idle: Whether to wait for network idle after navigation.
        """
        url = f"{base_url.rstrip('/')}{path}"
        page.goto(url)
        if wait_for_idle:
            page.wait_for_load_state("networkidle")

    return _goto
