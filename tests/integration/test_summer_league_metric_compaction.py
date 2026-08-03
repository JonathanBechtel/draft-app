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


@pytest.mark.asyncio
async def test_legacy_day_partition_uses_publication_stamp_not_source_currency(
    db_session: AsyncSession,
) -> None:
    """Rows predating ``effective_day`` group by their Eastern publication day.

    Legacy rows have no event day, so retention derives one from ``published_at``
    in Eastern time -- never from ``as_of``, which is source currency. Version 1
    is published at 02:00 UTC on July 2, which is still July 1 in Eastern time,
    while its ``as_of`` falls on July 2: it therefore owns its own daily
    partition and survives, and only the superseded July 2 row is removed.
    """
    competition = SummerLeagueCompetition(
        year=2026,
        league_id="compaction-legacy-day",
        venue_slug="las_vegas",
        display_name="Compaction Legacy Day",
    )
    player = make_player("Legacy", "Stamp")
    db_session.add_all([competition, player])
    await db_session.flush()
    assert competition.id is not None
    assert player.id is not None

    rows = {
        # (version, published_at, as_of); every row is superseded and published.
        1: (datetime(2026, 7, 2, 2), datetime(2026, 7, 2, 12)),
        2: (datetime(2026, 7, 2, 12), datetime(2026, 7, 2, 13)),
        3: (datetime(2026, 7, 2, 13), datetime(2026, 7, 2, 14)),
    }
    for version, (published_at, as_of) in rows.items():
        db_session.add(
            SummerLeagueMetricContext(
                competition_id=competition.id,
                year=2026,
                venue_slug="las_vegas",
                version=version,
                is_current=False,
                effective_day=None,
                as_of=as_of,
                published_at=published_at,
            )
        )
        db_session.add(
            SummerLeaguePlayerSeason(
                competition_id=competition.id,
                player_id=player.id,
                year=2026,
                venue_slug="las_vegas",
                version=version,
                is_current=False,
                effective_day=None,
                as_of=as_of,
                published_at=published_at,
            )
        )
    await db_session.commit()

    async with db_session.begin():
        summary = await compact_metric_versions(
            db_session,
            now=datetime(2026, 7, 5, 12, tzinfo=timezone.utc),
        )

    assert summary.context_rows_deleted == 1
    assert summary.season_rows_deleted == 1
    surviving_contexts = (
        (
            await db_session.execute(
                select(SummerLeagueMetricContext.version).where(
                    SummerLeagueMetricContext.competition_id == competition.id
                )
            )
        )
        .scalars()
        .all()
    )
    surviving_seasons = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason.version).where(
                    SummerLeaguePlayerSeason.competition_id == competition.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(surviving_contexts) == {1, 3}
    assert set(surviving_seasons) == {1, 3}


@pytest.mark.asyncio
async def test_compaction_preserves_archive_over_later_ordinary_rebuild(
    db_session: AsyncSession,
) -> None:
    """A historical close survives two later ordinary rebuilds of its event day."""
    competition = SummerLeagueCompetition(
        year=2026,
        league_id="compaction-archive",
        venue_slug="las_vegas",
        display_name="Compaction Archive",
    )
    player = make_player("Archive", "Close")
    db_session.add_all([competition, player])
    await db_session.flush()
    assert competition.id is not None
    assert player.id is not None

    effective_day = date(2026, 7, 10)
    for version, is_archival, is_current in (
        (1, True, False),
        (2, False, False),
        (3, False, True),
    ):
        published_at = datetime(2026, 7, 11, version)
        db_session.add(
            SummerLeagueMetricContext(
                competition_id=competition.id,
                year=2026,
                venue_slug="las_vegas",
                version=version,
                is_current=is_current,
                is_archival=is_archival,
                effective_day=effective_day,
                published_at=published_at,
            )
        )
        db_session.add(
            SummerLeaguePlayerSeason(
                competition_id=competition.id,
                player_id=player.id,
                year=2026,
                venue_slug="las_vegas",
                version=version,
                is_current=is_current,
                is_archival=is_archival,
                effective_day=effective_day,
                published_at=published_at,
            )
        )
    await db_session.commit()

    async with db_session.begin():
        summary = await compact_metric_versions(
            db_session,
            now=datetime(2026, 7, 12, 12, tzinfo=timezone.utc),
        )

    assert summary.context_rows_deleted == 1
    assert summary.season_rows_deleted == 1
    context_versions = (
        (
            await db_session.execute(
                select(
                    SummerLeagueMetricContext.version,
                    SummerLeagueMetricContext.is_archival,
                ).where(SummerLeagueMetricContext.competition_id == competition.id)
            )
        )
        .tuples()
        .all()
    )
    season_versions = (
        (
            await db_session.execute(
                select(
                    SummerLeaguePlayerSeason.version,
                    SummerLeaguePlayerSeason.is_archival,
                ).where(SummerLeaguePlayerSeason.competition_id == competition.id)
            )
        )
        .tuples()
        .all()
    )
    assert set(context_versions) == {(1, True), (3, False)}
    assert set(season_versions) == {(1, True), (3, False)}


@pytest.mark.asyncio
async def test_legacy_rows_without_an_effective_day_stay_retention_eligible(
    db_session: AsyncSession,
) -> None:
    """Compaction falls back to the Eastern ``published_at`` day, unlike the read."""
    competition = SummerLeagueCompetition(
        year=2026,
        league_id="compaction-legacy-day",
        venue_slug="las_vegas",
        display_name="Compaction Legacy Day",
    )
    player = make_player("Legacy", "Day")
    db_session.add_all([competition, player])
    await db_session.flush()
    assert competition.id is not None
    assert player.id is not None
    # Both rows predate the ``effective_day`` column, so they are invisible to the
    # trend read; retention must still see them as one closed Eastern day.
    for version in (1, 2):
        db_session.add(
            SummerLeaguePlayerSeason(
                competition_id=competition.id,
                player_id=player.id,
                year=2026,
                venue_slug="las_vegas",
                version=version,
                is_current=False,
                effective_day=None,
                as_of=datetime(2026, 7, 6, 12),
                published_at=datetime(2026, 7, 6, 13 + version),
                gmsc=float(version),
            )
        )
    await db_session.commit()

    async with db_session.begin():
        closed = await compact_metric_versions(
            db_session,
            now=datetime(2026, 7, 8, 12, tzinfo=timezone.utc),
        )

    assert closed.season_rows_deleted == 1
    survivors = (
        (
            await db_session.execute(
                select(SummerLeaguePlayerSeason).where(
                    SummerLeaguePlayerSeason.competition_id == competition.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.version for row in survivors] == [2]
    assert survivors[0].effective_day is None
