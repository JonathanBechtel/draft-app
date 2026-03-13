"""Visual tests for the Film Room page."""

from playwright.sync_api import Page, expect


class TestFilmRoomVisuals:
    """Visual checks for the Film Room page."""

    def test_film_room_page_loads(self, page: Page, goto) -> None:
        """Verify the Film Room page and channel sidebar render."""
        goto("/film-room")

        expect(page.locator(".film-page")).to_be_visible()
        expect(page.locator(".film-room-sidebar")).to_be_visible()
        expect(page.locator(".film-room-sidebar__item").first).to_contain_text(
            "All Channels"
        )

    def test_film_room_full_screenshot(self, page: Page, goto, screenshot) -> None:
        """Capture the Film Room page for visual review."""
        goto("/film-room")
        screenshot.capture_full_page("film_room_full")

