"""Visual regression tests for homepage news section redesign."""

import os

import pytest
from playwright.sync_api import Page, expect

# Get base URL from environment or default to 8080
BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8080")


@pytest.fixture(scope="session")
def browser_context_args():
    """Configure browser viewport for consistent screenshots."""
    return {"viewport": {"width": 1280, "height": 800}}


def test_homepage_loads(page: Page):
    """Verify homepage loads and key sections are visible."""
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")

    # Verify Top Prospects section exists
    expect(page.locator("#prospectsGrid")).to_be_visible()

    # Verify VS Arena section exists
    expect(page.locator(".h2h-card")).to_be_visible()

    # Verify news grid section exists
    expect(page.locator("#articlesGrid")).to_be_visible()


def test_homepage_full_screenshot(page: Page):
    """Capture full homepage screenshot for visual review."""
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")

    # Wait a bit for any async JS rendering
    page.wait_for_timeout(500)

    page.screenshot(path="screenshots/homepage_full.png", full_page=True)


def test_news_hero_section(page: Page):
    """Capture news hero section screenshot."""
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)

    hero = page.locator("#newsHeroSection")

    # Hero may be hidden if no articles with images exist
    if hero.is_visible():
        hero.screenshot(path="screenshots/news_hero.png")
    else:
        # Take screenshot of where hero would be
        page.screenshot(path="screenshots/news_hero_hidden.png", full_page=False)


def test_news_grid_section(page: Page):
    """Capture news grid and sidebar section screenshot."""
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)

    # Scroll to news grid section
    page.locator("#articlesGrid").scroll_into_view_if_needed()
    page.wait_for_timeout(200)

    # Capture the main layout (grid + sidebar)
    main_layout = page.locator(".main-layout")
    if main_layout.is_visible():
        main_layout.screenshot(path="screenshots/news_grid_sidebar.png")
    else:
        # Fallback to just the grid
        page.locator("#articlesGrid").screenshot(path="screenshots/news_grid.png")


def test_sidebar_visible_on_desktop(page: Page):
    """Verify sidebar is visible on desktop viewport."""
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")

    sidebar = page.locator(".sidebar")
    expect(sidebar).to_be_visible()

    # Take screenshot of sidebar
    sidebar.screenshot(path="screenshots/sidebar_desktop.png")


def test_sidebar_hidden_on_tablet(page: Page):
    """Verify sidebar is hidden on tablet viewport."""
    # Set tablet viewport
    page.set_viewport_size({"width": 900, "height": 800})

    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")

    sidebar = page.locator(".sidebar")
    expect(sidebar).not_to_be_visible()

    # Take screenshot of grid without sidebar
    page.locator(".main-layout").screenshot(path="screenshots/grid_tablet.png")


def test_responsive_mobile(page: Page):
    """Capture mobile view screenshot."""
    page.set_viewport_size({"width": 375, "height": 667})

    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)

    page.screenshot(path="screenshots/homepage_mobile.png", full_page=True)


def test_pagination_visible(page: Page):
    """Verify pagination controls are present."""
    page.goto(BASE_URL)
    page.wait_for_load_state("networkidle")

    pagination = page.locator("#pagination")

    # Pagination may be hidden if fewer than 6 articles
    if pagination.is_visible():
        pagination.screenshot(path="screenshots/pagination.png")
