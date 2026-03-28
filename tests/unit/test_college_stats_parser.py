"""Unit tests for college stats HTML parsing from Basketball-Reference.

Tests the parse_college_stats_html function using the existing Lonzo Ball
fixture and synthetic HTML for edge cases.
"""

from pathlib import Path

import pytest

from app.services.college_stats_service import parse_college_stats_html

FIXTURE_PATH = Path("scrapers/bbref/player_page_example.html")


@pytest.fixture
def lonzo_html() -> str:
    """Load the Lonzo Ball BBRef fixture page."""
    return FIXTURE_PATH.read_text(encoding="utf-8", errors="ignore")


def _wrap_college_table(tbody_rows: str) -> str:
    """Build a minimal BBRef page with a college stats table in a comment."""
    table = f"""
    <div class="table_container" id="div_all_college_stats">
    <table class="sortable stats_table" id="all_college_stats">
    <thead><tr>
        <th data-stat="season">Season</th>
        <th data-stat="g">G</th>
    </tr></thead>
    <tbody>{tbody_rows}</tbody>
    </table>
    </div>
    """
    # Wrap in the expected comment structure
    return f"""
    <html><body>
    <div id="all_all_college_stats" class="table_wrapper setup_commented commented">
    <!--{table}-->
    </div>
    </body></html>
    """


class TestLonzoBallFixture:
    """Parse the real Lonzo Ball fixture page."""

    def test_returns_one_season(self, lonzo_html: str) -> None:
        """Lonzo played one college season at UCLA."""
        rows = parse_college_stats_html(lonzo_html)
        assert len(rows) == 1

    def test_season_label(self, lonzo_html: str) -> None:
        """Season should be '2016-17'."""
        rows = parse_college_stats_html(lonzo_html)
        assert rows[0].season == "2016-17"

    def test_games(self, lonzo_html: str) -> None:
        """Lonzo played 36 games."""
        rows = parse_college_stats_html(lonzo_html)
        assert rows[0].games == 36

    def test_per_game_from_bbref(self, lonzo_html: str) -> None:
        """Verify per-game stats provided directly by BBRef."""
        row = parse_college_stats_html(lonzo_html)[0]
        assert row.mpg == 35.1
        assert row.ppg == 14.6
        assert row.rpg == 6.0
        assert row.apg == 7.6

    def test_computed_per_game(self, lonzo_html: str) -> None:
        """Verify per-game stats computed from totals (stl=66, blk=28, etc.)."""
        row = parse_college_stats_html(lonzo_html)[0]
        # 66 stl / 36 g = 1.833... → 1.8
        assert row.spg == 1.8
        # 28 blk / 36 g = 0.777... → 0.8
        assert row.bpg == 0.8
        # 89 tov / 36 g = 2.472... → 2.5
        assert row.tov == 2.5
        # 65 pf / 36 g = 1.805... → 1.8
        assert row.pf == 1.8

    def test_computed_attempts_per_game(self, lonzo_html: str) -> None:
        """Verify attempts-per-game computed from totals."""
        row = parse_college_stats_html(lonzo_html)[0]
        # 194 fg3a / 36 g = 5.388... → 5.4
        assert row.three_pa == 5.4
        # 98 fta / 36 g = 2.722... → 2.7
        assert row.fta == 2.7

    def test_percentage_conversion(self, lonzo_html: str) -> None:
        """BBRef .551 should become 55.1 display format."""
        row = parse_college_stats_html(lonzo_html)[0]
        assert row.fg_pct == 55.1
        assert row.three_p_pct == 41.2
        assert row.ft_pct == 67.3

    def test_games_started_is_none(self, lonzo_html: str) -> None:
        """BBRef college table doesn't include games started."""
        row = parse_college_stats_html(lonzo_html)[0]
        assert row.games_started is None


class TestMultiSeason:
    """Test parsing a player with multiple college seasons."""

    def test_multi_season_returns_all(self) -> None:
        """A 4-year player should return 4 rows."""
        rows_html = ""
        for year in range(2019, 2023):
            end = (year + 1) % 100
            season = f"{year}-{end:02d}"
            rows_html += f"""
            <tr>
                <th data-stat="season">{season}</th>
                <td data-stat="g">30</td>
                <td data-stat="mp">900</td>
                <td data-stat="fg">100</td><td data-stat="fga">200</td>
                <td data-stat="fg3">30</td><td data-stat="fg3a">80</td>
                <td data-stat="ft">50</td><td data-stat="fta">60</td>
                <td data-stat="orb">20</td><td data-stat="trb">150</td>
                <td data-stat="ast">120</td><td data-stat="stl">30</td>
                <td data-stat="blk">15</td><td data-stat="tov">60</td>
                <td data-stat="pf">60</td><td data-stat="pts">280</td>
                <td data-stat="fg_pct">.500</td>
                <td data-stat="fg3_pct">.375</td>
                <td data-stat="ft_pct">.833</td>
                <td data-stat="mp_per_g">30.0</td>
                <td data-stat="pts_per_g">9.3</td>
                <td data-stat="trb_per_g">5.0</td>
                <td data-stat="ast_per_g">4.0</td>
            </tr>
            """
        html = _wrap_college_table(rows_html)
        rows = parse_college_stats_html(html)
        assert len(rows) == 4
        assert [r.season for r in rows] == [
            "2019-20",
            "2020-21",
            "2021-22",
            "2022-23",
        ]


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_no_college_stats_table(self) -> None:
        """Page without college stats returns empty list."""
        html = "<html><body><div>No stats here</div></body></html>"
        assert parse_college_stats_html(html) == []

    def test_empty_comment(self) -> None:
        """Wrapper div exists but comment has no table."""
        html = """
        <html><body>
        <div id="all_all_college_stats">
        <!-- no table here -->
        </div>
        </body></html>
        """
        assert parse_college_stats_html(html) == []

    def test_zero_games(self) -> None:
        """Player with 0 games should have None for per-game stats."""
        row_html = """
        <tr>
            <th data-stat="season">2023-24</th>
            <td data-stat="g">0</td>
            <td data-stat="mp">0</td>
            <td data-stat="fg">0</td><td data-stat="fga">0</td>
            <td data-stat="fg3">0</td><td data-stat="fg3a">0</td>
            <td data-stat="ft">0</td><td data-stat="fta">0</td>
            <td data-stat="orb">0</td><td data-stat="trb">0</td>
            <td data-stat="ast">0</td><td data-stat="stl">0</td>
            <td data-stat="blk">0</td><td data-stat="tov">0</td>
            <td data-stat="pf">0</td><td data-stat="pts">0</td>
            <td data-stat="fg_pct"></td>
            <td data-stat="fg3_pct"></td>
            <td data-stat="ft_pct"></td>
            <td data-stat="mp_per_g"></td>
            <td data-stat="pts_per_g"></td>
            <td data-stat="trb_per_g"></td>
            <td data-stat="ast_per_g"></td>
        </tr>
        """
        html = _wrap_college_table(row_html)
        rows = parse_college_stats_html(html)
        assert len(rows) == 1
        row = rows[0]
        assert row.games == 0
        assert row.spg is None
        assert row.bpg is None
        assert row.tov is None
        assert row.pf is None
        assert row.three_pa is None
        assert row.fta is None

    def test_missing_columns(self) -> None:
        """Row with missing stat cells returns None for those fields."""
        row_html = """
        <tr>
            <th data-stat="season">2023-24</th>
            <td data-stat="g">10</td>
            <td data-stat="pts_per_g">15.0</td>
        </tr>
        """
        html = _wrap_college_table(row_html)
        rows = parse_college_stats_html(html)
        assert len(rows) == 1
        row = rows[0]
        assert row.games == 10
        assert row.ppg == 15.0
        assert row.rpg is None
        assert row.apg is None
        assert row.fg_pct is None

    def test_career_row_skipped(self) -> None:
        """The 'Career' footer row should not appear in results."""
        row_html = """
        <tr>
            <th data-stat="season">2022-23</th>
            <td data-stat="g">30</td>
            <td data-stat="pts_per_g">10.0</td>
            <td data-stat="trb_per_g">5.0</td>
            <td data-stat="ast_per_g">3.0</td>
            <td data-stat="mp_per_g">25.0</td>
        </tr>
        """
        # Career row has season text "Career" which won't match YYYY-YY pattern
        html = _wrap_college_table(row_html)
        rows = parse_college_stats_html(html)
        assert len(rows) == 1
        assert rows[0].season == "2022-23"

    def test_invalid_season_format_skipped(self) -> None:
        """Rows with non-standard season format are skipped."""
        row_html = """
        <tr>
            <th data-stat="season">Career</th>
            <td data-stat="g">120</td>
        </tr>
        <tr>
            <th data-stat="season">2022-23</th>
            <td data-stat="g">30</td>
            <td data-stat="pts_per_g">10.0</td>
            <td data-stat="trb_per_g">5.0</td>
            <td data-stat="ast_per_g">3.0</td>
            <td data-stat="mp_per_g">25.0</td>
        </tr>
        """
        html = _wrap_college_table(row_html)
        rows = parse_college_stats_html(html)
        assert len(rows) == 1
        assert rows[0].season == "2022-23"
