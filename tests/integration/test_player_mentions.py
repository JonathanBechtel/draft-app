"""Integration tests for player mention resolution and junction table."""

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_lifecycle import CareerStatus, DraftStatus, PlayerLifecycle
from app.schemas.players_master import PlayerMaster
from app.services.player_mention_service import resolve_player_names


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
    stmt = select(PlayerMaster).where(PlayerMaster.id == results[0].player_id)
    row = (await db_session.execute(stmt)).scalar_one()
    assert row.is_stub is True
    assert row.first_name == "Totally"
    assert row.middle_name == "New"
    assert row.last_name == "Player"

    alias_stmt = select(PlayerAlias).where(PlayerAlias.player_id == row.id)
    alias = (await db_session.execute(alias_stmt)).scalar_one()
    assert alias.full_name == "Totally New Player"
    assert alias.context == "mention_resolution"

    lifecycle_stmt = select(PlayerLifecycle).where(PlayerLifecycle.player_id == row.id)
    lifecycle = (await db_session.execute(lifecycle_stmt)).scalar_one()
    assert lifecycle.career_status == CareerStatus.PROSPECT
    assert lifecycle.draft_status == DraftStatus.UNKNOWN
    assert lifecycle.expected_draft_year is None
    assert lifecycle.is_draft_prospect is True


@pytest.mark.asyncio
async def test_stub_parses_suffix_into_player_fields(db_session: AsyncSession) -> None:
    """Stub creation should store recognized suffixes separately."""
    results = await resolve_player_names(
        db_session, ["Walter A. Clayton Jr"], create_stubs=True
    )
    assert len(results) == 1

    stmt = select(PlayerMaster).where(PlayerMaster.id == results[0].player_id)
    row = (await db_session.execute(stmt)).scalar_one()
    assert row.first_name == "Walter"
    assert row.middle_name == "A."
    assert row.last_name == "Clayton"
    assert row.suffix == "Jr."


@pytest.mark.asyncio
async def test_stub_gets_slug(db_session: AsyncSession) -> None:
    """Stub players should get auto-generated slugs via the before_insert listener."""
    results = await resolve_player_names(
        db_session, ["Brand New Prospect"], create_stubs=True
    )
    assert len(results) == 1

    stmt = select(PlayerMaster).where(PlayerMaster.id == results[0].player_id)
    row = (await db_session.execute(stmt)).scalar_one()
    assert row.slug is not None
    assert "brand-new-prospect" in row.slug


@pytest.mark.asyncio
async def test_stub_uses_expected_draft_year_in_lifecycle_not_master(
    db_session: AsyncSession,
) -> None:
    """Projected stub class should live in lifecycle, not factual draft fields."""
    results = await resolve_player_names(
        db_session, ["Future Prospect"], draft_year=2027, create_stubs=True
    )
    assert len(results) == 1

    stmt = select(PlayerMaster).where(PlayerMaster.id == results[0].player_id)
    row = (await db_session.execute(stmt)).scalar_one()
    assert row.draft_year is None

    lifecycle_stmt = select(PlayerLifecycle).where(PlayerLifecycle.player_id == row.id)
    lifecycle = (await db_session.execute(lifecycle_stmt)).scalar_one()
    assert lifecycle.expected_draft_year == 2027
    assert lifecycle.is_draft_prospect is True


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
async def test_resolve_relaxed_suffix_variant(
    db_session: AsyncSession,
) -> None:
    """Suffix-only name differences should resolve to an existing player."""
    player = PlayerMaster(
        first_name="Darius",
        last_name="Acuff",
        suffix="Jr.",
        display_name="Darius Acuff Jr.",
        draft_year=2026,
        is_stub=False,
    )
    db_session.add(player)
    await db_session.flush()

    results = await resolve_player_names(
        db_session, ["Darius Acuff"], create_stubs=True
    )
    assert len(results) == 1
    assert results[0].player_id == player.id
    assert results[0].matched_via == "display_name"

    count_stmt = select(func.count()).select_from(PlayerMaster)
    total_players = (await db_session.execute(count_stmt)).scalar_one()
    assert total_players == 1


@pytest.mark.asyncio
async def test_resolve_relaxed_alias_variant(
    db_session: AsyncSession,
) -> None:
    """Middle initial and punctuation variants should resolve via alias."""
    player = PlayerMaster(
        first_name="Walter",
        last_name="Clayton",
        suffix="Jr.",
        display_name="Walter Clayton Jr.",
        draft_year=2025,
        is_stub=False,
    )
    db_session.add(player)
    await db_session.flush()
    db_session.add(
        PlayerAlias(
            player_id=player.id,  # type: ignore[arg-type]
            full_name="Walter A. Clayton Jr.",
            first_name="Walter",
            middle_name="A.",
            last_name="Clayton",
            suffix="Jr.",
        )
    )
    await db_session.flush()

    results = await resolve_player_names(
        db_session,
        ["Walter Clayton Jr", "Walter Clayton"],
        create_stubs=True,
    )
    assert len(results) == 1
    assert results[0].player_id == player.id


@pytest.mark.asyncio
async def test_ambiguous_relaxed_match_does_not_create_stub(
    db_session: AsyncSession,
) -> None:
    """Ambiguous relaxed matches should be skipped instead of creating a duplicate."""
    db_session.add_all(
        [
            PlayerMaster(
                first_name="John",
                middle_name="A.",
                last_name="Smith",
                display_name="John A. Smith",
                is_stub=False,
            ),
            PlayerMaster(
                first_name="John",
                last_name="Smith",
                suffix="Jr.",
                display_name="John Smith Jr.",
                is_stub=False,
            ),
        ]
    )
    await db_session.flush()

    results = await resolve_player_names(db_session, ["John Smith"], create_stubs=True)
    assert results == []

    count_stmt = select(func.count()).select_from(PlayerMaster)
    total_players = (await db_session.execute(count_stmt)).scalar_one()
    assert total_players == 2


@pytest.mark.asyncio
async def test_no_stub_when_disabled(db_session: AsyncSession) -> None:
    """With create_stubs=False, unknown names should be skipped."""
    results = await resolve_player_names(
        db_session, ["Unknown Player X"], create_stubs=False
    )
    assert len(results) == 0


@pytest.mark.asyncio
async def test_single_token_unknown_name_does_not_create_stub(
    db_session: AsyncSession,
) -> None:
    """Single-token mentions should be skipped instead of creating junk stubs."""
    results = await resolve_player_names(db_session, ["Lendeborg"], create_stubs=True)
    assert results == []

    count_stmt = select(func.count()).select_from(PlayerMaster)
    total_players = (await db_session.execute(count_stmt)).scalar_one()
    assert total_players == 0


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
