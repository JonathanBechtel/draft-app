"""Integration tests for the x_threads angle picker and dedup behavior."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.combine_anthro import CombineAnthro
from app.schemas.players_master import PlayerMaster
from app.schemas.seasons import Season
from app.schemas.x_post_history import XPostAngle, XPostHistory, XPostStatus
from app.services.x_threads.angle_picker import pick_angle


async def _seed_player_with_anthro(
    db: AsyncSession,
    *,
    name: str,
    draft_year: int,
    season_id: int,
    wingspan_in: float = 84.0,
    height_w_shoes_in: float = 78.0,
) -> PlayerMaster:
    player = PlayerMaster(
        first_name=name.split()[0],
        last_name=name.split()[-1],
        display_name=name,
        draft_year=draft_year,
        is_stub=False,
    )
    db.add(player)
    await db.flush()
    anthro = CombineAnthro(
        player_id=player.id,
        season_id=season_id,
        wingspan_in=wingspan_in,
        height_w_shoes_in=height_w_shoes_in,
    )
    db.add(anthro)
    await db.flush()
    return player


async def _seed_season(db: AsyncSession) -> int:
    season = Season(code="2025-26", start_year=2025, end_year=2026)
    db.add(season)
    await db.flush()
    assert season.id is not None
    return season.id


@pytest.mark.asyncio
async def test_pick_angle_returns_spotlight_for_current_class(
    db_session: AsyncSession,
) -> None:
    """With one viable player, the spotlight angle should be chosen."""
    season_id = await _seed_season(db_session)
    await _seed_player_with_anthro(
        db_session, name="Test Player", draft_year=2026, season_id=season_id
    )

    pick = await pick_angle(
        db_session,
        preferred_angle=XPostAngle.spotlight,
    )
    assert pick is not None
    assert pick.angle == "spotlight"
    assert pick.players[0].display_name == "Test Player"


@pytest.mark.asyncio
async def test_pick_angle_respects_recent_history(
    db_session: AsyncSession,
) -> None:
    """A spotlighted player in the dedup window should not be re-picked."""
    season_id = await _seed_season(db_session)
    player_a = await _seed_player_with_anthro(
        db_session, name="Player Aaa", draft_year=2026, season_id=season_id
    )
    player_b = await _seed_player_with_anthro(
        db_session, name="Player Bbb", draft_year=2026, season_id=season_id
    )

    # Pretend Player A was just spotlighted.
    db_session.add(
        XPostHistory(
            angle=XPostAngle.spotlight,
            status=XPostStatus.draft,
            player_ids=[player_a.id],
            tweets=[{"text": "lead"}],
            image_paths=[],
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
    )
    await db_session.flush()

    pick = await pick_angle(
        db_session,
        preferred_angle=XPostAngle.spotlight,
    )
    assert pick is not None
    assert pick.players[0].id == player_b.id


@pytest.mark.asyncio
async def test_pick_angle_returns_none_when_pool_exhausted(
    db_session: AsyncSession,
) -> None:
    """All angles return None when every candidate has been spotlighted recently."""
    season_id = await _seed_season(db_session)
    player = await _seed_player_with_anthro(
        db_session, name="Solo Prospect", draft_year=2026, season_id=season_id
    )
    db_session.add(
        XPostHistory(
            angle=XPostAngle.spotlight,
            status=XPostStatus.draft,
            player_ids=[player.id],
            tweets=[{"text": "lead"}],
            image_paths=[],
            created_at=datetime.utcnow() - timedelta(hours=1),
        )
    )
    await db_session.flush()

    pick = await pick_angle(
        db_session,
        preferred_angle=XPostAngle.spotlight,
    )
    assert pick is None


@pytest.mark.asyncio
async def test_pick_angle_window_expires(db_session: AsyncSession) -> None:
    """History older than the dedup window no longer blocks the candidate."""
    season_id = await _seed_season(db_session)
    player = await _seed_player_with_anthro(
        db_session, name="Old Pick", draft_year=2026, season_id=season_id
    )
    db_session.add(
        XPostHistory(
            angle=XPostAngle.spotlight,
            status=XPostStatus.draft,
            player_ids=[player.id],
            tweets=[{"text": "lead"}],
            image_paths=[],
            created_at=datetime.utcnow() - timedelta(days=30),
        )
    )
    await db_session.flush()

    pick = await pick_angle(
        db_session,
        window_days=14,
        preferred_angle=XPostAngle.spotlight,
    )
    assert pick is not None
    assert pick.players[0].id == player.id
