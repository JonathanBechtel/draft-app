"""Integration tests for the public create_stub_player service wrapper.

Exercises all four outcome branches against a real Postgres test schema.
Requires TEST_DATABASE_URL and PYTEST_ALLOW_DB=1.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_lifecycle import CareerStatus, DraftStatus, PlayerLifecycle
from app.schemas.players_master import PlayerMaster
from app.services.player_mention_service import create_stub_player


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _add_player(
    db: AsyncSession,
    first_name: str,
    last_name: str,
    suffix: str | None = None,
    display_name: str | None = None,
) -> PlayerMaster:
    """Insert and flush a minimal PlayerMaster row."""
    dn = display_name or (f"{first_name} {last_name}" + (f" {suffix}" if suffix else ""))
    player = PlayerMaster(
        first_name=first_name,
        last_name=last_name,
        suffix=suffix,
        display_name=dn,
        is_stub=False,
    )
    db.add(player)
    await db.flush()
    return player


async def _add_alias(db: AsyncSession, player_id: int, full_name: str) -> None:
    """Insert and flush a PlayerAlias row."""
    db.add(PlayerAlias(player_id=player_id, full_name=full_name))
    await db.flush()


# ---------------------------------------------------------------------------
# Outcome: rejected_guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_name_rejected(db_session: AsyncSession) -> None:
    """An empty name must return rejected_guard without touching the DB."""
    result = await create_stub_player(db_session, "")
    assert result.outcome == "rejected_guard"
    assert result.reason is not None

    count = (
        await db_session.execute(select(func.count()).select_from(PlayerMaster))
    ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_single_token_name_rejected(db_session: AsyncSession) -> None:
    """Single-token names must be rejected — too vague to be a unique player."""
    result = await create_stub_player(db_session, "Flagg")
    assert result.outcome == "rejected_guard"
    assert result.player_id is None

    count = (
        await db_session.execute(select(func.count()).select_from(PlayerMaster))
    ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_suffix_only_name_rejected(db_session: AsyncSession) -> None:
    """A single real-name token plus suffix must be rejected."""
    result = await create_stub_player(db_session, "Cooper Jr.")
    assert result.outcome == "rejected_guard"


# ---------------------------------------------------------------------------
# Outcome: blocked_existing (unique match)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_on_exact_display_name_match(db_session: AsyncSession) -> None:
    """Exact display_name match must block creation and return the existing player."""
    existing = await _add_player(db_session, "Cooper", "Flagg")

    result = await create_stub_player(db_session, "Cooper Flagg")

    assert result.outcome == "blocked_existing"
    assert result.match is not None
    assert result.match.player_id == existing.id
    assert result.player_id is None

    # No new player was created.
    count = (
        await db_session.execute(select(func.count()).select_from(PlayerMaster))
    ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_blocked_on_relaxed_suffix_variant(db_session: AsyncSession) -> None:
    """A suffix-stripped variant of an existing player must block creation."""
    existing = await _add_player(db_session, "Darius", "Acuff", suffix="Jr.", display_name="Darius Acuff Jr.")

    result = await create_stub_player(db_session, "Darius Acuff")

    assert result.outcome == "blocked_existing"
    assert result.match is not None
    assert result.match.player_id == existing.id

    count = (
        await db_session.execute(select(func.count()).select_from(PlayerMaster))
    ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_blocked_on_alias_match(db_session: AsyncSession) -> None:
    """A name matching a PlayerAlias must block creation."""
    existing = await _add_player(db_session, "Dylan", "Harper")
    await _add_alias(db_session, existing.id, "D.J. Harper")  # type: ignore[arg-type]

    result = await create_stub_player(db_session, "D.J. Harper")

    assert result.outcome == "blocked_existing"
    assert result.match is not None
    assert result.match.player_id == existing.id

    count = (
        await db_session.execute(select(func.count()).select_from(PlayerMaster))
    ).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# Outcome: ambiguous (multiple matches)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ambiguous_returns_candidates_no_creation(db_session: AsyncSession) -> None:
    """When multiple players match the relaxed key, return ambiguous with candidates."""
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

    result = await create_stub_player(db_session, "John Smith")

    assert result.outcome == "ambiguous"
    assert result.candidates is not None
    assert len(result.candidates) >= 2
    candidate_ids = {c.player_id for c in result.candidates}
    assert len(candidate_ids) >= 2
    assert result.player_id is None

    # No third player was created.
    count = (
        await db_session.execute(select(func.count()).select_from(PlayerMaster))
    ).scalar_one()
    assert count == 2


# ---------------------------------------------------------------------------
# Outcome: created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_name_creates_stub_player(db_session: AsyncSession) -> None:
    """A genuinely new two-token name must create a stub with alias + lifecycle."""
    result = await create_stub_player(db_session, "Totally New Prospect")

    assert result.outcome == "created"
    assert result.player_id is not None

    # Verify the PlayerMaster row.
    row = (
        await db_session.execute(
            select(PlayerMaster).where(PlayerMaster.id == result.player_id)
        )
    ).scalar_one()
    assert row.is_stub is True
    assert row.display_name == "Totally New Prospect"
    assert row.first_name == "Totally"
    assert row.last_name == "Prospect"

    # Alias row created.
    alias = (
        await db_session.execute(
            select(PlayerAlias).where(PlayerAlias.player_id == result.player_id)
        )
    ).scalar_one()
    assert alias.full_name == "Totally New Prospect"
    assert alias.context == "mention_resolution"

    # Lifecycle row created.
    lifecycle = (
        await db_session.execute(
            select(PlayerLifecycle).where(PlayerLifecycle.player_id == result.player_id)
        )
    ).scalar_one()
    assert lifecycle.career_status == CareerStatus.PROSPECT
    assert lifecycle.draft_status == DraftStatus.UNKNOWN
    assert lifecycle.is_draft_prospect is True


@pytest.mark.asyncio
async def test_draft_year_stored_in_lifecycle(db_session: AsyncSession) -> None:
    """draft_year must flow through to the lifecycle row, not the master row."""
    result = await create_stub_player(db_session, "Future Star", draft_year=2028)

    assert result.outcome == "created"
    assert result.player_id is not None

    master = (
        await db_session.execute(
            select(PlayerMaster).where(PlayerMaster.id == result.player_id)
        )
    ).scalar_one()
    assert master.draft_year is None  # never set on master for stubs

    lifecycle = (
        await db_session.execute(
            select(PlayerLifecycle).where(PlayerLifecycle.player_id == result.player_id)
        )
    ).scalar_one()
    assert lifecycle.expected_draft_year == 2028


@pytest.mark.asyncio
async def test_created_stub_gets_slug(db_session: AsyncSession) -> None:
    """Stub players must receive auto-generated slugs via the before_insert listener."""
    result = await create_stub_player(db_session, "Brand New Star")

    assert result.outcome == "created"
    master = (
        await db_session.execute(
            select(PlayerMaster).where(PlayerMaster.id == result.player_id)
        )
    ).scalar_one()
    assert master.slug is not None
    assert "brand-new-star" in master.slug


@pytest.mark.asyncio
async def test_no_regression_on_existing_ingestion_path(
    db_session: AsyncSession,
) -> None:
    """Calling create_stub_player must not affect existing resolve_player_names behavior."""
    from app.services.player_mention_service import resolve_player_names

    existing = await _add_player(db_session, "Cooper", "Flagg")

    # The public wrapper sees the existing player and blocks.
    wrapper_result = await create_stub_player(db_session, "Cooper Flagg")
    assert wrapper_result.outcome == "blocked_existing"

    # Ingestion path (resolve_player_names) still returns the existing player normally.
    matches = await resolve_player_names(db_session, ["Cooper Flagg"], create_stubs=False)
    assert len(matches) == 1
    assert matches[0].player_id == existing.id

    count = (
        await db_session.execute(select(func.count()).select_from(PlayerMaster))
    ).scalar_one()
    assert count == 1
