"""Visual checks for school logo rendering.

Captures full-page and element screenshots of player and stats pages to
verify school logos appear inline next to school names.
"""

from playwright.sync_api import Page


PLAYERS_WITH_LOGOS = ["jayden-quaintance", "nikolas-khamenia"]
PLAYERS_WITHOUT_LOGO = ["eli-ndiaye"]  # Real Madrid — no NCAA logo


class TestPlayerDetailLogo:
    """Verify the player-detail header and college-stats row show logos."""

    def test_player_detail_with_logo(self, page: Page, goto, screenshot) -> None:
        """Render a player whose school has a logo (Kentucky)."""
        goto(f"/players/{PLAYERS_WITH_LOGOS[0]}")
        page.wait_for_load_state("networkidle")
        screenshot.capture_full_page("player_detail_with_logo")
        screenshot.capture_element(
            ".player-primary-meta", "player_detail_logo_meta"
        )

    def test_player_detail_duke_logo(self, page: Page, goto, screenshot) -> None:
        """Render a Duke player to confirm logo on a second school."""
        goto(f"/players/{PLAYERS_WITH_LOGOS[1]}")
        page.wait_for_load_state("networkidle")
        screenshot.capture_element(
            ".player-primary-meta", "player_detail_logo_duke"
        )

    def test_player_detail_without_logo(self, page: Page, goto, screenshot) -> None:
        """Player whose school has no logo should still render cleanly."""
        goto(f"/players/{PLAYERS_WITHOUT_LOGO[0]}")
        page.wait_for_load_state("networkidle")
        screenshot.capture_element(
            ".player-primary-meta", "player_detail_no_logo"
        )


class TestStatsMetricLogos:
    """Verify the metric leaderboard shows logos in cards and table rows."""

    def test_stats_metric_full(self, page: Page, goto, screenshot) -> None:
        """Capture the wingspan leaderboard end-to-end."""
        goto("/stats/wingspan_in")
        page.wait_for_load_state("networkidle")
        screenshot.capture_full_page("stats_metric_full")
        # Element-level captures of the three summary cards + the table
        page.locator(".stat-summary-row").first.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        page.locator(".stat-summary-row").first.screenshot(
            path="tests/visual/screenshots/stats_metric_summary_row.png"
        )
        page.locator(".stats-leaderboard, .leaderboard, table").first.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        page.locator("table").first.screenshot(
            path="tests/visual/screenshots/stats_metric_table.png"
        )


class TestStatsDraftYearLogos:
    """Verify the draft-year grid shows logos in winner cards."""

    def test_stats_draft_year_full(self, page: Page, goto, screenshot) -> None:
        """Capture the 2025 draft-year grid (winner cards + table)."""
        goto("/stats/combine/2025")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)  # let JS render winners
        screenshot.capture_full_page("stats_draft_year_full")
        winners = page.locator("#dy-winners-grid")
        if winners.is_visible():
            winners.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            winners.screenshot(
                path="tests/visual/screenshots/stats_draft_year_winners.png"
            )
        table = page.locator("#dy-table-body").first
        if table.is_visible():
            table.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            table.screenshot(
                path="tests/visual/screenshots/stats_draft_year_table.png"
            )
