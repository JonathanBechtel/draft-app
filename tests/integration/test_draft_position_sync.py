"""Integration tests for ``sync_draft_positions``.

The Summer League Explorer filters on ``players_master.draft_round/draft_pick``,
but the draft-night ingest only writes ``draft_results``. These tests exercise
the bridge that copies resolved picks onto the player, and guard the edges that
matter for correctness: undrafted/unresolved rows are left alone, the team
abbreviation is derived, and the per-year filter is respected.

Guard: integration tests require ``TEST_DATABASE_URL`` and ``PYTEST_ALLOW_DB=1``
— see ``tests/integration/conftest.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.draft_results import DraftResult
from app.schemas.nba_teams import NbaTeam
from app.schemas.players_master import PlayerMaster
from app.services.draft_position_sync_service import sync_draft_positions
from tests.integration.conftest import make_player


async def _team(db: AsyncSession, abbr: str) -> NbaTeam:
    t = NbaTeam(name=f"{abbr} Team", abbreviation=abbr, slug=abbr.lower())
    db.add(t)
    await db.flush()
    return t


async def _player(db: AsyncSession, first: str, last: str) -> PlayerMaster:
    p = make_player(first, last)
    p.draft_round = None
    p.draft_pick = None
    db.add(p)
    await db.flush()
    return p


async def _pick(
    db: AsyncSession,
    *,
    draft_year: int,
    overall: int,
    player_id: int | None,
    team_id: int | None,
    raw_name: str,
    raw_team: str | None = None,
) -> DraftResult:
    round_no = 1 if overall <= 30 else 2
    dr = DraftResult(
        draft_year=draft_year,
        overall_pick=overall,
        round=round_no,
        round_pick=overall if round_no == 1 else overall - 30,
        player_id=player_id,
        team_id=team_id,
        raw_player_name=raw_name,
        raw_team=raw_team,
        resolution_method="matched" if player_id else "unresolved",
        source="test",
    )
    db.add(dr)
    await db.flush()
    return dr


@pytest.mark.asyncio
async def test_sync_populates_round_pick_and_team(db_session: AsyncSession) -> None:
    """A resolved pick stamps within-round round/pick and the team abbr.

    Given a player with NULL draft round/pick and a matching draft_results row
    (overall pick 35 → round 2, pick 5), the sync fills draft_round=2,
    draft_pick=5, and draft_team from the selecting team's abbreviation.
    """
    team = await _team(db_session, "BOS")
    player = await _player(db_session, "Rookie", "One")
    assert player.id is not None and team.id is not None
    await _pick(
        db_session,
        draft_year=2026,
        overall=35,
        player_id=player.id,
        team_id=team.id,
        raw_name="Rookie One",
        raw_team="BOS",
    )

    updated = await sync_draft_positions(db_session, draft_year=2026)
    await db_session.refresh(player)

    assert updated == 1
    assert player.draft_year == 2026
    assert player.draft_round == 2
    assert player.draft_pick == 5
    assert player.draft_team == "BOS"


@pytest.mark.asyncio
async def test_sync_leaves_undrafted_and_unresolved_alone(
    db_session: AsyncSession,
) -> None:
    """Undrafted players and unresolved picks keep NULL round/pick.

    An unresolved draft_results row (player_id is NULL) must not error and must
    not touch any player; a player with no pick at all stays NULL.
    """
    undrafted = await _player(db_session, "Undrafted", "Guy")
    await _pick(
        db_session,
        draft_year=2026,
        overall=40,
        player_id=None,  # unresolved
        team_id=None,
        raw_name="Mystery Prospect",
    )

    updated = await sync_draft_positions(db_session, draft_year=2026)
    await db_session.refresh(undrafted)

    assert updated == 0
    assert undrafted.draft_round is None
    assert undrafted.draft_pick is None


@pytest.mark.asyncio
async def test_sync_falls_back_to_raw_team_without_team_id(
    db_session: AsyncSession,
) -> None:
    """When team_id is unmatched, draft_team falls back to the raw token."""
    player = await _player(db_session, "Rookie", "Two")
    assert player.id is not None
    await _pick(
        db_session,
        draft_year=2026,
        overall=3,
        player_id=player.id,
        team_id=None,
        raw_name="Rookie Two",
        raw_team="PHX",
    )

    await sync_draft_positions(db_session, draft_year=2026)
    await db_session.refresh(player)

    assert player.draft_round == 1
    assert player.draft_pick == 3
    assert player.draft_team == "PHX"


@pytest.mark.asyncio
async def test_sync_respects_draft_year_filter(db_session: AsyncSession) -> None:
    """A year-scoped sync only touches that year's picks.

    With picks in two years, ``sync(draft_year=2026)`` updates only the 2026
    player and leaves the 2025 player's NULL round/pick untouched; a subsequent
    unscoped sync (``draft_year=None``) then fills both.
    """
    team = await _team(db_session, "NYK")
    p2026 = await _player(db_session, "Class", "TwentySix")
    p2025 = await _player(db_session, "Class", "TwentyFive")
    assert p2026.id is not None and p2025.id is not None and team.id is not None
    await _pick(
        db_session,
        draft_year=2026,
        overall=10,
        player_id=p2026.id,
        team_id=team.id,
        raw_name="Class TwentySix",
    )
    await _pick(
        db_session,
        draft_year=2025,
        overall=12,
        player_id=p2025.id,
        team_id=team.id,
        raw_name="Class TwentyFive",
    )

    scoped = await sync_draft_positions(db_session, draft_year=2026)
    await db_session.refresh(p2026)
    await db_session.refresh(p2025)
    assert scoped == 1
    assert p2026.draft_pick == 10
    assert p2025.draft_pick is None

    everything = await sync_draft_positions(db_session, draft_year=None)
    await db_session.refresh(p2025)
    assert everything == 2  # both rows now match
    assert p2025.draft_pick == 12
