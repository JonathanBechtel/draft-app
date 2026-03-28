"""Integration tests for the college stats scraping service.

Tests upsert logic, idempotency, and the eligibility query against
a real Postgres database using the shared integration test fixtures.
"""

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_college_stats import PlayerCollegeStats
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.players_master import PlayerMaster
from app.services.college_stats_service import (
    CollegeSeasonRow,
    _find_eligible_players,
    upsert_college_stats,
)
from tests.integration.conftest import make_player

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_row(season: str = "2023-24", ppg: float = 18.5) -> CollegeSeasonRow:
    """Build a sample CollegeSeasonRow for testing."""
    return CollegeSeasonRow(
        season=season,
        games=32,
        mpg=34.2,
        ppg=ppg,
        rpg=6.1,
        apg=3.4,
        spg=1.2,
        bpg=0.8,
        tov=2.1,
        pf=1.9,
        fg_pct=48.3,
        three_p_pct=37.5,
        three_pa=5.2,
        ft_pct=81.0,
        fta=4.8,
    )


async def _create_player_with_bbr(
    db: AsyncSession,
    first_name: str,
    last_name: str,
    school: str | None,
    bbr_slug: str | None,
) -> PlayerMaster:
    """Create a player and optionally add a BBRef external ID."""
    player = make_player(first_name, last_name, school=school)
    db.add(player)
    await db.flush()

    if bbr_slug is not None:
        ext_id = PlayerExternalId(
            player_id=player.id,  # type: ignore[arg-type]
            system="bbr",
            external_id=bbr_slug,
            source_url=f"https://www.basketball-reference.com/players/{bbr_slug[0]}/{bbr_slug}.html",
        )
        db.add(ext_id)
        await db.flush()

    return player


# ---------------------------------------------------------------------------
# Upsert tests
# ---------------------------------------------------------------------------


class TestUpsertCollegeStats:
    """Test upsert_college_stats persistence logic."""

    async def test_creates_rows(self, db_session: AsyncSession) -> None:
        """Upsert should create new rows in player_college_stats."""
        player = make_player("Test", "Player", school="Duke")
        db_session.add(player)
        await db_session.flush()
        pid = player.id
        assert pid is not None

        rows = [_sample_row("2022-23", ppg=15.0), _sample_row("2023-24", ppg=18.5)]
        count = await upsert_college_stats(db_session, pid, rows)
        await db_session.commit()

        assert count == 2

        result = await db_session.execute(
            select(PlayerCollegeStats)  # type: ignore[call-overload]
            .where(PlayerCollegeStats.player_id == pid)  # type: ignore[arg-type]
            .order_by(PlayerCollegeStats.season)
        )
        db_rows = result.scalars().all()
        assert len(db_rows) == 2
        assert db_rows[0].season == "2022-23"
        assert db_rows[0].ppg == 15.0
        assert db_rows[1].season == "2023-24"
        assert db_rows[1].ppg == 18.5
        assert db_rows[0].source == "sports_reference"
        assert db_rows[1].source == "sports_reference"

    async def test_idempotent(self, db_session: AsyncSession) -> None:
        """Upserting the same data twice produces no duplicates."""
        player = make_player("Idem", "Potent", school="UNC")
        db_session.add(player)
        await db_session.flush()
        pid = player.id
        assert pid is not None

        rows = [_sample_row("2023-24")]

        await upsert_college_stats(db_session, pid, rows)
        await db_session.commit()
        await upsert_college_stats(db_session, pid, rows)
        await db_session.commit()

        result = await db_session.execute(
            select(PlayerCollegeStats).where(  # type: ignore[call-overload]
                PlayerCollegeStats.player_id == pid  # type: ignore[arg-type]
            )
        )
        assert len(result.scalars().all()) == 1

    async def test_overwrites_ai_generated(self, db_session: AsyncSession) -> None:
        """BBRef data should overwrite existing AI-generated stats."""
        player = make_player("Over", "Write", school="Kentucky")
        db_session.add(player)
        await db_session.flush()
        pid = player.id
        assert pid is not None

        # Insert AI-generated data first
        ai_stats = PlayerCollegeStats(
            player_id=pid,
            season="2023-24",
            ppg=20.0,
            source="ai_generated",
        )
        db_session.add(ai_stats)
        await db_session.commit()

        # Upsert with BBRef data
        rows = [_sample_row("2023-24", ppg=18.5)]
        await upsert_college_stats(db_session, pid, rows)
        await db_session.commit()

        result = await db_session.execute(
            select(PlayerCollegeStats).where(  # type: ignore[call-overload]
                PlayerCollegeStats.player_id == pid  # type: ignore[arg-type]
            )
        )
        db_row = result.scalar_one()
        assert db_row.ppg == 18.5
        assert db_row.source == "sports_reference"

    async def test_empty_rows_returns_zero(self, db_session: AsyncSession) -> None:
        """Upserting an empty list returns 0."""
        player = make_player("Empty", "List", school="Stanford")
        db_session.add(player)
        await db_session.flush()
        pid = player.id
        assert pid is not None

        count = await upsert_college_stats(db_session, pid, [])
        assert count == 0


# ---------------------------------------------------------------------------
# Eligibility query tests
# ---------------------------------------------------------------------------


class TestFindEligiblePlayers:
    """Test _find_eligible_players query logic."""

    async def test_includes_college_player_with_bbr_id(
        self, db_session: AsyncSession
    ) -> None:
        """Player with school + BBRef ID should be eligible."""
        await _create_player_with_bbr(
            db_session, "Eli", "Gible", school="Duke", bbr_slug="gibleel01"
        )
        await db_session.commit()

        players = await _find_eligible_players(db_session)
        assert len(players) == 1
        assert players[0][1] == "Eli Gible"
        assert players[0][2] == "gibleel01"

    async def test_excludes_no_school(self, db_session: AsyncSession) -> None:
        """Player without a school (international) should be excluded."""
        await _create_player_with_bbr(
            db_session, "Intl", "Player", school=None, bbr_slug="playein01"
        )
        await db_session.commit()

        players = await _find_eligible_players(db_session)
        assert len(players) == 0

    async def test_excludes_no_bbr_id(self, db_session: AsyncSession) -> None:
        """Player without a BBRef external ID should be excluded."""
        await _create_player_with_bbr(
            db_session, "No", "BbrId", school="UCLA", bbr_slug=None
        )
        await db_session.commit()

        players = await _find_eligible_players(db_session)
        assert len(players) == 0

    async def test_only_missing_excludes_already_scraped(
        self, db_session: AsyncSession
    ) -> None:
        """With only_missing=True, skip players who have sports_reference stats."""
        player = await _create_player_with_bbr(
            db_session, "Already", "Scraped", school="Duke", bbr_slug="scraal01"
        )
        # Add existing sports_reference stats
        stats = PlayerCollegeStats(
            player_id=player.id,  # type: ignore[arg-type]
            season="2023-24",
            ppg=15.0,
            source="sports_reference",
        )
        db_session.add(stats)
        await db_session.commit()

        players = await _find_eligible_players(db_session, only_missing=True)
        assert len(players) == 0

        # Without only_missing, should still be included
        players_all = await _find_eligible_players(db_session, only_missing=False)
        assert len(players_all) == 1

    async def test_player_id_filter(self, db_session: AsyncSession) -> None:
        """Filtering by player_id restricts results to one player."""
        p1 = await _create_player_with_bbr(
            db_session, "First", "Player", school="Duke", bbr_slug="playefi01"
        )
        await _create_player_with_bbr(
            db_session, "Second", "Player", school="UNC", bbr_slug="playese01"
        )
        await db_session.commit()

        players = await _find_eligible_players(
            db_session, player_id=p1.id  # type: ignore[arg-type]
        )
        assert len(players) == 1
        assert players[0][1] == "First Player"
