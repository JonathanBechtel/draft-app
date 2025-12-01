"""Integration tests for PlayerMaster slug auto-generation."""

import pytest

from app.schemas.players_master import PlayerMaster


@pytest.mark.asyncio
async def test_slug_auto_generated_from_display_name(db_session):
    """Slug is automatically generated from display_name on insert."""
    player = PlayerMaster(display_name="Cooper Flagg", school="Duke")
    db_session.add(player)
    await db_session.commit()
    await db_session.refresh(player)

    assert player.slug == "cooper-flagg"


@pytest.mark.asyncio
async def test_slug_collision_appends_suffix(db_session):
    """When slug already exists, a numeric suffix is appended."""
    # First player gets base slug
    player1 = PlayerMaster(display_name="John Smith", school="Duke")
    db_session.add(player1)
    await db_session.commit()
    await db_session.refresh(player1)

    # Second player with same name gets -2 suffix
    player2 = PlayerMaster(display_name="John Smith", school="Kentucky")
    db_session.add(player2)
    await db_session.commit()
    await db_session.refresh(player2)

    # Third player gets -3 suffix
    player3 = PlayerMaster(display_name="John Smith", school="UCLA")
    db_session.add(player3)
    await db_session.commit()
    await db_session.refresh(player3)

    assert player1.slug == "john-smith"
    assert player2.slug == "john-smith-2"
    assert player3.slug == "john-smith-3"


@pytest.mark.asyncio
async def test_explicit_slug_preserved(db_session):
    """Explicitly set slug is not overridden by auto-generation."""
    player = PlayerMaster(
        display_name="Cooper Flagg",
        slug="custom-slug",
        school="Duke",
    )
    db_session.add(player)
    await db_session.commit()
    await db_session.refresh(player)

    assert player.slug == "custom-slug"


@pytest.mark.asyncio
async def test_no_slug_when_display_name_empty(db_session):
    """No slug generated when display_name is None or empty."""
    player = PlayerMaster(display_name=None, school="Unknown")
    db_session.add(player)
    await db_session.commit()
    await db_session.refresh(player)

    assert player.slug is None


@pytest.mark.asyncio
async def test_slug_normalizes_special_characters(db_session):
    """Slug handles unicode and special characters correctly."""
    player = PlayerMaster(display_name="José García Jr.", school="Arizona")
    db_session.add(player)
    await db_session.commit()
    await db_session.refresh(player)

    assert player.slug == "jose-garcia-jr"
