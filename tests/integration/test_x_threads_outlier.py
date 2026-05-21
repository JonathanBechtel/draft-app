"""Integration test for the plus-wingspan outlier path.

The other outlier finders (split_profile, elite_metric) depend on the metric
snapshot chain, which has its own dedicated tests. plus_wingspan only needs
combine_anthro rows, so it's the cheapest way to validate the end-to-end
finder dispatch from a seeded fixture.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.combine_anthro import CombineAnthro
from app.schemas.players_master import PlayerMaster
from app.schemas.seasons import Season
from app.services.x_threads.outlier_finder import (
    find_outlier_candidate,
    _plus_wingspan,
)


async def _seed_pool(db: AsyncSession) -> list[int]:
    season = Season(code="2025-26", start_year=2025, end_year=2026)
    db.add(season)
    await db.flush()

    profiles = [
        ("Average Guy", 82.0, 79.0),
        ("Stretchy Wing", 88.0, 80.0),  # +8" plus-wingspan: the outlier
        ("Standard Wing", 82.5, 80.0),
        ("Tall Guard", 80.0, 78.0),
        ("Compact Player", 79.0, 78.5),
        ("Big Body", 84.0, 82.5),
    ]
    ids: list[int] = []
    for name, wing, height in profiles:
        player = PlayerMaster(
            first_name=name.split()[0],
            last_name=name.split()[-1],
            display_name=name,
            draft_year=2026,
            is_stub=False,
        )
        db.add(player)
        await db.flush()
        anthro = CombineAnthro(
            player_id=player.id,
            season_id=season.id,
            wingspan_in=wing,
            height_w_shoes_in=height,
        )
        db.add(anthro)
        assert player.id is not None
        ids.append(player.id)
    await db.flush()
    return ids


@pytest.mark.asyncio
async def test_plus_wingspan_finds_the_top_diff_player(
    db_session: AsyncSession,
) -> None:
    """The stretchy wing has the biggest plus-wingspan; it should land in the top quintile."""
    pool = await _seed_pool(db_session)
    result = await _plus_wingspan(db_session, pool)
    assert result is not None
    assert result.subtype == "plus_wingspan"
    # With 6 players the top 20% = 1 candidate, so the result is deterministic.
    assert result.player.display_name == "Stretchy Wing"
    # Wingspan and height labels should both be present.
    labels = {stat.label for stat in result.stats}
    assert "Wingspan" in labels
    assert "Plus-wingspan" in labels


@pytest.mark.asyncio
async def test_find_outlier_candidate_filters_excluded_players(
    db_session: AsyncSession,
) -> None:
    """Players in the excluded set should never come back from the finder."""
    pool = await _seed_pool(db_session)
    excluded = set(pool)
    result = await find_outlier_candidate(db_session, excluded_player_ids=excluded)
    assert result is None
