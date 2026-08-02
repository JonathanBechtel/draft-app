"""Integration coverage for bounded Summer League metric-version retention."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import SummerLeagueCompetition
from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeaguePlayerSeason,
)
from app.services.summer_league.metric_compaction import compact_metric_versions
from tests.integration.conftest import make_player


async def _seed_versions(db: AsyncSession) -> int:
    """Seed one competition with daily versions, a current row, and candidates."""
    competition = SummerLeagueCompetition(
        year=2026,
        league_id="compaction-test",
        venue_slug="las_vegas",
        display_name="Compaction Test",
    )
    players = [make_player("Daily", "One"), make_player("Daily", "Two")]
    db.add(competition)
    db.add_all(players)
    await db.flush()
    assert competition.id is not None
    assert all(player.id is not None for player in players)

    version_dates = {
        1: datetime(2026, 7, 1, 12),
        2: datetime(2026, 7, 1, 18),
        3: datetime(2026, 7, 2, 12),
        4: datetime(2026, 7, 3, 12),
        5: datetime(2026, 7, 4, 12),
        6: datetime(2026, 7, 4, 18),
        10: datetime(2026, 7, 5, 12),
        # The current source day is deliberately left uncompactable.
        11: datetime(2026, 7, 6, 8),
        12: datetime(2026, 7, 6, 9),
    }
    published_versions = {1, 2, 3, 4, 5, 10}
    for version, as_of in version_dates.items():
        is_current = version == 10
        db.add(
            SummerLeagueMetricContext(
                competition_id=competition.id,
                year=2026,
                venue_slug="las_vegas",
                version=version,
                is_current=is_current,
                as_of=as_of,
                published_at=as_of if version in published_versions else None,
            )
        )
        for player in players:
            assert player.id is not None
            db.add(
                SummerLeaguePlayerSeason(
                    competition_id=competition.id,
                    player_id=player.id,
                    year=2026,
                    venue_slug="las_vegas",
                    version=version,
                    is_current=is_current,
                    as_of=as_of,
                    published_at=as_of if version in published_versions else None,
                    model_version=f"fit-{version}",
                )
            )
    await db.flush()
    return competition.id


@pytest.mark.asyncio
async def test_compaction_keeps_published_close_current_and_inflight_candidate(
    db_session: AsyncSession,
) -> None:
    """Closed-day duplicates are removed without losing the trend series or candidate."""
    competition_id = await _seed_versions(db_session)
    await db_session.commit()

    async with db_session.begin():
        summary = await compact_metric_versions(
            db_session,
            now=datetime(2026, 7, 6, 12, tzinfo=timezone.utc),
        )

    assert summary.cutoff == datetime(2026, 7, 6)
    assert summary.context_rows_deleted == 1
    assert summary.season_rows_deleted == 2
    assert summary.rows_deleted == 3

    contexts = (
        (
            await db_session.execute(
                select(SummerLeagueMetricContext)
                .where(SummerLeagueMetricContext.competition_id == competition_id)
                .order_by(SummerLeagueMetricContext.version)
            )
        )
        .scalars()
        .all()
    )
    assert [context.version for context in contexts] == [2, 3, 4, 5, 6, 10, 11, 12]
    assert [context.version for context in contexts if context.is_current] == [10]
    assert (
        next(context for context in contexts if context.version == 5).published_at
        is not None
    )
    assert (
        next(context for context in contexts if context.version == 6).published_at
        is None
    )

    seasons = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason)
                .where(SummerLeaguePlayerSeason.competition_id == competition_id)
                .order_by(
                    SummerLeaguePlayerSeason.player_id,
                    SummerLeaguePlayerSeason.version,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {season.version for season in seasons} == {2, 3, 4, 5, 6, 10, 11, 12}
    assert [season.version for season in seasons if season.is_current] == [10, 10]

    # The daily history remains readable at the player-season grain after compaction.
    assert {
        season.as_of.date()
        for season in seasons
        if season.version in {2, 3, 4, 5, 6, 10}
    } == {
        datetime(2026, 7, 1).date(),
        datetime(2026, 7, 2).date(),
        datetime(2026, 7, 3).date(),
        datetime(2026, 7, 4).date(),
        datetime(2026, 7, 5).date(),
    }

    await db_session.commit()
    async with db_session.begin():
        rerun = await compact_metric_versions(
            db_session,
            now=datetime(2026, 7, 6, 12, tzinfo=timezone.utc),
        )
    assert rerun.rows_deleted == 0


@pytest.mark.asyncio
async def test_effective_day_cutoff_uses_eastern_calendar_boundary(
    db_session: AsyncSession,
) -> None:
    """A UTC run just after midnight does not close the still-current ET day."""
    competition = SummerLeagueCompetition(
        year=2026,
        league_id="compaction-effective-day",
        venue_slug="las_vegas",
        display_name="Compaction Effective Day",
    )
    player = make_player("Effective", "Day")
    db_session.add_all([competition, player])
    await db_session.flush()
    assert competition.id is not None
    assert player.id is not None
    effective_day = date(2026, 7, 6)
    for version, is_current in ((1, False), (2, False), (3, True)):
        db_session.add(
            SummerLeaguePlayerSeason(
                competition_id=competition.id,
                player_id=player.id,
                year=2026,
                venue_slug="las_vegas",
                version=version,
                is_current=is_current,
                effective_day=effective_day,
                as_of=datetime(2026, 8, version, 12),
                published_at=datetime(2026, 8, version, 13),
                gmsc=float(version),
            )
        )
    await db_session.commit()

    async with db_session.begin():
        still_open = await compact_metric_versions(
            db_session,
            now=datetime(2026, 7, 7, 1, tzinfo=timezone.utc),
        )
    assert still_open.season_rows_deleted == 0

    await db_session.commit()
    async with db_session.begin():
        closed = await compact_metric_versions(
            db_session,
            now=datetime(2026, 7, 7, 5, tzinfo=timezone.utc),
        )
    assert closed.season_rows_deleted == 1
