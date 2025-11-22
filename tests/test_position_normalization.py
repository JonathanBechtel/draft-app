import pytest
from sqlalchemy import select
from app.schemas.positions import Position
from app.schemas.player_status import PlayerStatus
from scripts.ingest_player_bios import _upsert_status
from scripts.ingest_combine import get_or_create_position_id
from app.models.position_taxonomy import derive_position_tags


@pytest.mark.asyncio
async def test_position_creation(db_session):
    # Test creating a position
    pos = Position(code="PG", description="Point Guard", parents=["guard"])
    db_session.add(pos)
    await db_session.commit()
    await db_session.refresh(pos)
    assert pos.id is not None
    assert pos.code == "PG"
    assert "guard" in pos.parents


@pytest.mark.asyncio
async def test_derive_position_tags():
    # Test taxonomy logic
    assert derive_position_tags("Point Guard") == ("pg", ["guard"])
    assert derive_position_tags("PG/SG") == ("pg_sg", ["guard"])


@pytest.mark.asyncio
async def test_ingest_player_bios_position_resolution(db_session):
    # Mock row data
    class MockRow:
        is_active_nba = True
        current_team = "Team A"
        nba_last_season = "2023-24"
        position = "Point Guard"
        height_in = 72
        weight_lb = 180

    row = MockRow()
    # player_id = 1  # Assuming player exists or we mock it, but _upsert_status needs a player_id
    # )

    # Create dummy player status to update or insert
    # We need to ensure player_id exists in master if foreign key constraint is enforced
    # But for unit test with sqlite, maybe we can skip if we don't enforce FK,
    # but better to create a player.
    from app.schemas.players_master import PlayerMaster

    pm = PlayerMaster(first_name="Test", last_name="Player")
    db_session.add(pm)
    await db_session.commit()

    await _upsert_status(db_session, pm.id, row)
    await db_session.commit()

    # Verify
    stmt = select(PlayerStatus).where(PlayerStatus.player_id == pm.id)
    status = (await db_session.execute(stmt)).scalar_one()
    assert status.raw_position == "Point Guard"
    assert status.position_id is not None

    # Verify position created
    stmt = select(Position).where(Position.id == status.position_id)
    pos = (await db_session.execute(stmt)).scalar_one()
    assert pos.code == "pg"
    assert "guard" in pos.parents


@pytest.mark.asyncio
async def test_ingest_combine_position_resolution(db_session):
    pos_id = await get_or_create_position_id(db_session, "sf")
    assert pos_id is not None

    # Check it exists
    stmt = select(Position).where(Position.id == pos_id)
    pos = (await db_session.execute(stmt)).scalar_one()
    assert pos.code == "sf"

    # Get again
    pos_id2 = await get_or_create_position_id(db_session, "sf")
    assert pos_id == pos_id2
