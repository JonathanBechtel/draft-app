"""Visual regression tests for the film-room page."""

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

class TestFilmRoomStructure:
    """Tests verifying film-room page structure and key summary UI."""

    def test_film_room_loads(self, page: Page, goto) -> None:
        """Verify the film-room page renders its main shell."""
        goto("/film-room")

        expect(page.locator(".film-page")).to_be_visible()
        expect(page.locator(".film-room-page-stats")).to_be_visible()


class TestFilmRoomScreenshots:
    """Tests capturing film-room screenshots for visual review."""

    def test_film_room_full_screenshot(self, page: Page, goto, screenshot) -> None:
        """Capture the full film-room page for visual review."""
        goto("/film-room")
        screenshot.capture_full_page("film_room_full")

    def test_film_room_filtered_header(
        self, page: Page, goto, screenshot
    ) -> None:
        """Capture the filtered film-room header and summary cards."""
        goto("/film-room?tag=Highlights")
        screenshot.capture_element(".film-room-page-header", "film_room_header_highlights")
