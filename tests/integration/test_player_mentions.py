"""Integration tests for player mention resolution and junction table."""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_aliases import PlayerAlias
from app.schemas.players_master import PlayerMaster
from app.services.player_mention_service import (
    PlayerMatch,
    resolve_player_names,
)


@pytest_asyncio.fixture()
async def known_player(db_session: AsyncSession) -> PlayerMaster:
    """Insert a known player for matching tests."""
    player = PlayerMaster(
        first_name="Cooper",
        last_name="Flagg",
        display_name="Cooper Flagg",
        draft_year=2025,
        is_stub=False,
    )
    db_session.add(player)
    await db_session.flush()
    return player


@pytest_asyncio.fixture()
async def player_with_alias(db_session: AsyncSession) -> PlayerMaster:
    """Insert a player with an alias for alias-matching tests."""
    player = PlayerMaster(
        first_name="Dylan",
        last_name="Harper",
        display_name="Dylan Harper",
        draft_year=2025,
        is_stub=False,
    )
    db_session.add(player)
    await db_session.flush()

    alias = PlayerAlias(
        player_id=player.id,  # type: ignore[arg-type]
        full_name="D.J. Harper",
        first_name="D.J.",
        last_name="Harper",
    )
    db_session.add(alias)
    await db_session.flush()

    return player


@pytest.mark.asyncio
async def test_resolve_known_player_by_display_name(
    db_session: AsyncSession, known_player: PlayerMaster
) -> None:
    """Resolving an exact display_name match should return the known player."""
    results = await resolve_player_names(
        db_session, ["Cooper Flagg"], create_stubs=False
    )
    assert len(results) == 1
    assert results[0].player_id == known_player.id
    assert results[0].matched_via == "display_name"


@pytest.mark.asyncio
async def test_resolve_case_insensitive(
    db_session: AsyncSession, known_player: PlayerMaster
) -> None:
    """Name matching should be case-insensitive."""
    results = await resolve_player_names(
        db_session, ["cooper flagg"], create_stubs=False
    )
    assert len(results) == 1
    assert results[0].player_id == known_player.id


@pytest.mark.asyncio
async def test_resolve_via_alias(
    db_session: AsyncSession, player_with_alias: PlayerMaster
) -> None:
    """Should match via PlayerAlias when display_name doesn't match."""
    results = await resolve_player_names(
        db_session, ["D.J. Harper"], create_stubs=False
    )
    assert len(results) == 1
    assert results[0].player_id == player_with_alias.id
    assert results[0].matched_via == "alias"


@pytest.mark.asyncio
async def test_create_stub_for_unknown_name(db_session: AsyncSession) -> None:
    """Unknown names should create stub PlayerMaster records."""
    results = await resolve_player_names(
        db_session, ["Totally New Player"], create_stubs=True
    )
    assert len(results) == 1
    assert results[0].matched_via == "stub_created"
    assert results[0].display_name == "Totally New Player"

    # Verify the stub was created in the database
    stmt = select(PlayerMaster).where(
        PlayerMaster.id == results[0].player_id
    )
    row = (await db_session.execute(stmt)).scalar_one()
    assert row.is_stub is True
    assert row.first_name == "Totally"
    assert row.last_name == "New Player"


@pytest.mark.asyncio
async def test_stub_gets_slug(db_session: AsyncSession) -> None:
    """Stub players should get auto-generated slugs via the before_insert listener."""
    results = await resolve_player_names(
        db_session, ["Brand New Prospect"], create_stubs=True
    )
    assert len(results) == 1

    stmt = select(PlayerMaster).where(
        PlayerMaster.id == results[0].player_id
    )
    row = (await db_session.execute(stmt)).scalar_one()
    assert row.slug is not None
    assert "brand-new-prospect" in row.slug


@pytest.mark.asyncio
async def test_dedup_within_batch(
    db_session: AsyncSession, known_player: PlayerMaster
) -> None:
    """Duplicate names in a single batch should be deduplicated."""
    results = await resolve_player_names(
        db_session,
        ["Cooper Flagg", "Cooper Flagg", "cooper flagg"],
        create_stubs=False,
    )
    assert len(results) == 1
    assert results[0].player_id == known_player.id


@pytest.mark.asyncio
async def test_no_stub_when_disabled(db_session: AsyncSession) -> None:
    """With create_stubs=False, unknown names should be skipped."""
    results = await resolve_player_names(
        db_session, ["Unknown Player X"], create_stubs=False
    )
    assert len(results) == 0


@pytest.mark.asyncio
async def test_empty_names_list(db_session: AsyncSession) -> None:
    """Empty input should return empty results."""
    results = await resolve_player_names(db_session, [])
    assert results == []


@pytest.mark.asyncio
async def test_mixed_known_and_unknown(
    db_session: AsyncSession, known_player: PlayerMaster
) -> None:
    """Mix of known and unknown names should resolve correctly."""
    results = await resolve_player_names(
        db_session,
        ["Cooper Flagg", "Brand New Prospect"],
        create_stubs=True,
    )
    assert len(results) == 2
    ids = {r.player_id for r in results}
    assert known_player.id in ids
    # Second result should be a newly created stub
    stub = [r for r in results if r.matched_via == "stub_created"]
    assert len(stub) == 1
    assert stub[0].display_name == "Brand New Prospect"
