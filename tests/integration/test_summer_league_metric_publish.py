"""Integration coverage for ordered Summer League metric publication."""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import SummerLeagueEdition
from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeagueDerivedAgg,
)
from app.services.sources.summer_league.metric_publish import publish_metric_version
from tests.integration.conftest import make_player


@dataclass(frozen=True)
class _ProjectionSpec:
    """One context/player-season pair used by the publication test."""

    competition_id: int
    player_id: int
    version: int
    published_at: datetime | None
    is_current: bool


async def _projection_rows(
    db: AsyncSession,
    spec: _ProjectionSpec,
) -> None:
    """Add matching context and player-season rows for one publication version."""
    db.add(
        SummerLeagueMetricContext(
            competition_id=spec.competition_id,
            year=2026,
            venue_slug=f"venue-{spec.competition_id}",
            version=spec.version,
            is_current=spec.is_current,
            published_at=spec.published_at,
        )
    )
    db.add(
        SummerLeagueDerivedAgg(
            competition_id=spec.competition_id,
            player_id=spec.player_id,
            year=2026,
            venue_slug=f"venue-{spec.competition_id}",
            version=spec.version,
            is_current=spec.is_current,
            published_at=spec.published_at,
        )
    )


@pytest.mark.asyncio
async def test_older_full_flip_keeps_newer_scoped_publication_current(
    db_session: AsyncSession,
) -> None:
    """A late full flip cannot regress a newer scope and preserves audit timestamps."""
    competitions = [
        SummerLeagueEdition(
            year=2026,
            league_id="publish-guard-a",
            venue_slug="venue-a",
            display_name="Publish Guard A",
        ),
        SummerLeagueEdition(
            year=2026,
            league_id="publish-guard-b",
            venue_slug="venue-b",
            display_name="Publish Guard B",
        ),
    ]
    player = make_player("Publish", "Guard")
    db_session.add_all([*competitions, player])
    await db_session.flush()
    assert all(competition.id is not None for competition in competitions)
    assert player.id is not None
    competition_a_id = competitions[0].id
    competition_b_id = competitions[1].id
    assert competition_a_id is not None and competition_b_id is not None

    original_published_at = datetime(2026, 7, 29, 12)
    specs = [
        _ProjectionSpec(competition_a_id, player.id, 1, original_published_at, True),
        _ProjectionSpec(competition_a_id, player.id, 2, None, False),
        _ProjectionSpec(competition_a_id, player.id, 3, None, False),
        _ProjectionSpec(competition_b_id, player.id, 1, original_published_at, True),
        _ProjectionSpec(competition_b_id, player.id, 2, None, False),
    ]
    for spec in specs:
        await _projection_rows(db_session, spec)
    await db_session.commit()

    # The newer scoped Desk publication wins the race for competition A.
    async with db_session.begin():
        assert (
            await publish_metric_version(
                db_session,
                version=3,
                competition_ids={competition_a_id},
            )
            == set()
        )

    a_v3_published_at = (
        await db_session.execute(
            select(SummerLeagueMetricContext.published_at).where(
                SummerLeagueMetricContext.competition_id == competition_a_id,
                SummerLeagueMetricContext.version == 3,
            )
        )
    ).scalar_one()
    assert a_v3_published_at is not None
    await db_session.commit()

    # The older full rebuild may still publish competition B, but not A.
    async with db_session.begin():
        skipped_competition_ids = await publish_metric_version(db_session, version=2)
        assert skipped_competition_ids == {competition_a_id}

    contexts = (
        (
            await db_session.execute(
                select(SummerLeagueMetricContext)
                .where(
                    SummerLeagueMetricContext.competition_id.in_(  # type: ignore[attr-defined]
                        [competition_a_id, competition_b_id]
                    )
                )
                .order_by(
                    SummerLeagueMetricContext.competition_id,
                    SummerLeagueMetricContext.version,
                )
            )
        )
        .scalars()
        .all()
    )
    seasons = (
        (
            await db_session.execute(
                select(SummerLeagueDerivedAgg).where(
                    SummerLeagueDerivedAgg.competition_id.in_(  # type: ignore[attr-defined]
                        [competition_a_id, competition_b_id]
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    current_contexts = {
        context.competition_id: context for context in contexts if context.is_current
    }
    current_seasons = {
        season.competition_id: season for season in seasons if season.is_current
    }
    assert current_contexts[competition_a_id].version == 3
    assert current_seasons[competition_a_id].version == 3
    assert current_contexts[competition_a_id].published_at == a_v3_published_at
    assert current_seasons[competition_a_id].published_at == a_v3_published_at
    assert current_contexts[competition_b_id].version == 2
    assert current_seasons[competition_b_id].version == 2
    assert current_contexts[competition_b_id].published_at is not None
    assert current_seasons[competition_b_id].published_at is not None

    a_v1_context = next(
        context
        for context in contexts
        if context.competition_id == competition_a_id and context.version == 1
    )
    a_v1_season = next(
        season
        for season in seasons
        if season.competition_id == competition_a_id and season.version == 1
    )
    b_v1_context = next(
        context
        for context in contexts
        if context.competition_id == competition_b_id and context.version == 1
    )
    b_v1_season = next(
        season
        for season in seasons
        if season.competition_id == competition_b_id and season.version == 1
    )
    assert a_v1_context.published_at == original_published_at
    assert a_v1_season.published_at == original_published_at
    assert b_v1_context.published_at == original_published_at
    assert b_v1_season.published_at == original_published_at
    assert a_v1_context.is_current is False
    assert a_v1_season.is_current is False
    assert b_v1_context.is_current is False
    assert b_v1_season.is_current is False

    a_v2_context = next(
        context
        for context in contexts
        if context.competition_id == competition_a_id and context.version == 2
    )
    a_v2_season = next(
        season
        for season in seasons
        if season.competition_id == competition_a_id and season.version == 2
    )
    assert a_v2_context.published_at is None
    assert a_v2_season.published_at is None
