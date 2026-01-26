"""Integration tests for admin player-related sub-tables CRUD.

Tests PlayerStatus, PlayerAlias, and PlayerExternalId routes.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.positions import Position
from tests.integration.auth_helpers import create_auth_user, login_staff


ADMIN_EMAIL = "player-related-admin@example.com"
ADMIN_PASSWORD = "admin-password-123"


@pytest_asyncio.fixture
async def admin_user_id(db_session: AsyncSession) -> int:
    """Create an admin auth user for tests."""
    return await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=ADMIN_PASSWORD,
    )


@pytest_asyncio.fixture
async def sample_player_id(db_session: AsyncSession) -> int:
    """Create a sample player for tests."""
    player = PlayerMaster(
        display_name="Related Test Player",
        first_name="Related",
        last_name="Player",
        school="Test University",
        draft_year=2024,
    )
    db_session.add(player)
    await db_session.commit()
    await db_session.refresh(player)
    assert player.id is not None
    return player.id


@pytest_asyncio.fixture
async def sample_position_id(db_session: AsyncSession) -> int:
    """Create a sample position for dropdown tests."""
    position = Position(code="PG", description="Point Guard")
    db_session.add(position)
    await db_session.commit()
    await db_session.refresh(position)
    assert position.id is not None
    return position.id


@pytest.mark.asyncio
class TestPlayerStatusRoutes:
    """Tests for PlayerStatus CRUD routes."""

    async def test_status_form_renders(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """GET /admin/players/{id}/status shows the status form."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get(f"/admin/players/{sample_player_id}/status")
        assert response.status_code == 200
        assert "Player Status" in response.text
        assert "Related Test Player" in response.text
        assert 'name="height_in"' in response.text

    async def test_status_create(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
        sample_position_id: int,
    ):
        """POST /admin/players/{id}/status creates a new status."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/players/{sample_player_id}/status",
            data={
                "position_id": str(sample_position_id),
                "is_active_nba": "true",
                "current_team": "LAL",
                "height_in": "78",
                "weight_lb": "220",
                "source": "manual",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=saved" in response.headers.get("location", "")

        # Verify in database
        result = await db_session.execute(
            text("SELECT current_team, height_in FROM player_status WHERE player_id = :id"),
            {"id": sample_player_id},
        )
        row = result.one()
        assert row[0] == "LAL"
        assert row[1] == 78

    async def test_status_update(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id}/status updates existing status."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Create existing status
        status = PlayerStatus(
            player_id=sample_player_id,
            current_team="BOS",
            height_in=75,
        )
        db_session.add(status)
        await db_session.commit()

        response = await app_client.post(
            f"/admin/players/{sample_player_id}/status",
            data={
                "current_team": "MIA",
                "height_in": "76",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}

        # Verify update
        result = await db_session.execute(
            text("SELECT current_team, height_in FROM player_status WHERE player_id = :id"),
            {"id": sample_player_id},
        )
        row = result.one()
        assert row[0] == "MIA"
        assert row[1] == 76

    async def test_status_uncheck_is_active_nba(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id}/status can explicitly set is_active_nba to False."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Create status with is_active_nba=True
        status = PlayerStatus(
            player_id=sample_player_id,
            is_active_nba=True,
            current_team="LAL",
        )
        db_session.add(status)
        await db_session.commit()

        # Submit with checkbox unchecked (hidden input sends "false")
        response = await app_client.post(
            f"/admin/players/{sample_player_id}/status",
            data={
                "is_active_nba": "false",  # Hidden input value when checkbox unchecked
                "current_team": "LAL",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}

        # Verify is_active_nba is now False (not None)
        result = await db_session.execute(
            text("SELECT is_active_nba FROM player_status WHERE player_id = :id"),
            {"id": sample_player_id},
        )
        row = result.one()
        assert row[0] is False

    async def test_status_validation_error(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id}/status shows error for invalid height."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/players/{sample_player_id}/status",
            data={"height_in": "30"},  # Too low
        )
        assert response.status_code == 200
        assert "Height must be" in response.text

    async def test_status_delete(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id}/status/delete removes status."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Create status to delete
        status = PlayerStatus(player_id=sample_player_id, current_team="CHI")
        db_session.add(status)
        await db_session.commit()

        response = await app_client.post(
            f"/admin/players/{sample_player_id}/status/delete",
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}

        # Verify deleted
        result = await db_session.execute(
            text("SELECT id FROM player_status WHERE player_id = :id"),
            {"id": sample_player_id},
        )
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
class TestPlayerAliasRoutes:
    """Tests for PlayerAlias CRUD routes."""

    async def test_alias_list_renders(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """GET /admin/players/{id}/aliases shows the list page."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get(f"/admin/players/{sample_player_id}/aliases")
        assert response.status_code == 200
        assert "Player Aliases" in response.text

    async def test_alias_new_form_renders(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """GET /admin/players/{id}/aliases/new shows the form."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get(f"/admin/players/{sample_player_id}/aliases/new")
        assert response.status_code == 200
        assert "New Alias" in response.text
        assert 'name="full_name"' in response.text

    async def test_alias_create(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id}/aliases creates a new alias."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/players/{sample_player_id}/aliases",
            data={
                "full_name": "Test Alias Name",
                "first_name": "Test",
                "last_name": "Alias",
                "context": "manual",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=created" in response.headers.get("location", "")

        # Verify in database
        result = await db_session.execute(
            text("SELECT full_name FROM player_aliases WHERE player_id = :id"),
            {"id": sample_player_id},
        )
        assert result.scalar_one() == "Test Alias Name"

    async def test_alias_create_duplicate_error(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id}/aliases shows error for duplicate."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Create existing alias
        alias = PlayerAlias(player_id=sample_player_id, full_name="Existing Name")
        db_session.add(alias)
        await db_session.commit()

        response = await app_client.post(
            f"/admin/players/{sample_player_id}/aliases",
            data={"full_name": "Existing Name"},
        )
        assert response.status_code == 200
        assert "already exists" in response.text

    async def test_alias_update(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id}/aliases/{alias_id} updates alias."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Create alias to update
        alias = PlayerAlias(player_id=sample_player_id, full_name="Original Name")
        db_session.add(alias)
        await db_session.commit()
        await db_session.refresh(alias)
        alias_id = alias.id

        response = await app_client.post(
            f"/admin/players/{sample_player_id}/aliases/{alias_id}",
            data={"full_name": "Updated Name", "context": "updated"},
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}

        # Verify update
        result = await db_session.execute(
            text("SELECT full_name, context FROM player_aliases WHERE id = :id"),
            {"id": alias_id},
        )
        row = result.one()
        assert row[0] == "Updated Name"
        assert row[1] == "updated"

    async def test_alias_delete(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id}/aliases/{alias_id}/delete removes alias."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Create alias to delete
        alias = PlayerAlias(player_id=sample_player_id, full_name="To Delete")
        db_session.add(alias)
        await db_session.commit()
        await db_session.refresh(alias)
        alias_id = alias.id

        response = await app_client.post(
            f"/admin/players/{sample_player_id}/aliases/{alias_id}/delete",
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}

        # Verify deleted
        result = await db_session.execute(
            text("SELECT id FROM player_aliases WHERE id = :id"),
            {"id": alias_id},
        )
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
class TestPlayerExternalIdRoutes:
    """Tests for PlayerExternalId CRUD routes."""

    async def test_external_id_list_renders(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """GET /admin/players/{id}/external-ids shows the list page."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get(f"/admin/players/{sample_player_id}/external-ids")
        assert response.status_code == 200
        assert "External IDs" in response.text

    async def test_external_id_new_form_renders(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """GET /admin/players/{id}/external-ids/new shows the form."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get(
            f"/admin/players/{sample_player_id}/external-ids/new"
        )
        assert response.status_code == 200
        assert "New External ID" in response.text
        assert 'name="system"' in response.text

    async def test_external_id_create(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id}/external-ids creates a new external ID."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            f"/admin/players/{sample_player_id}/external-ids",
            data={
                "system": "nba_stats",
                "external_id": "12345",
                "source_url": "https://nba.com/player/12345",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=created" in response.headers.get("location", "")

        # Verify in database
        result = await db_session.execute(
            text(
                "SELECT system, external_id FROM player_external_ids "
                "WHERE player_id = :id"
            ),
            {"id": sample_player_id},
        )
        row = result.one()
        assert row[0] == "nba_stats"
        assert row[1] == "12345"

    async def test_external_id_create_duplicate_error(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id}/external-ids shows error for global duplicate."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Create another player with existing external ID
        other_player = PlayerMaster(
            display_name="Other Player",
            first_name="Other",
            last_name="Player",
        )
        db_session.add(other_player)
        await db_session.commit()
        await db_session.refresh(other_player)

        ext_id = PlayerExternalId(
            player_id=other_player.id,
            system="bbr",
            external_id="xyz123",
        )
        db_session.add(ext_id)
        await db_session.commit()

        # Try to create duplicate
        response = await app_client.post(
            f"/admin/players/{sample_player_id}/external-ids",
            data={"system": "bbr", "external_id": "xyz123"},
        )
        assert response.status_code == 200
        assert "already exists" in response.text

    async def test_external_id_update(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id}/external-ids/{ext_id} updates external ID."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Create external ID to update
        ext = PlayerExternalId(
            player_id=sample_player_id,
            system="espn",
            external_id="old123",
        )
        db_session.add(ext)
        await db_session.commit()
        await db_session.refresh(ext)
        ext_id = ext.id

        response = await app_client.post(
            f"/admin/players/{sample_player_id}/external-ids/{ext_id}",
            data={
                "system": "espn",
                "external_id": "new456",
                "source_url": "https://espn.com/player/new456",
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}

        # Verify update
        result = await db_session.execute(
            text(
                "SELECT external_id, source_url FROM player_external_ids WHERE id = :id"
            ),
            {"id": ext_id},
        )
        row = result.one()
        assert row[0] == "new456"
        assert row[1] == "https://espn.com/player/new456"

    async def test_external_id_delete(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """POST /admin/players/{id}/external-ids/{ext_id}/delete removes external ID."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Create external ID to delete
        ext = PlayerExternalId(
            player_id=sample_player_id,
            system="to_delete",
            external_id="delete_me",
        )
        db_session.add(ext)
        await db_session.commit()
        await db_session.refresh(ext)
        ext_id = ext.id

        response = await app_client.post(
            f"/admin/players/{sample_player_id}/external-ids/{ext_id}/delete",
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}

        # Verify deleted
        result = await db_session.execute(
            text("SELECT id FROM player_external_ids WHERE id = :id"),
            {"id": ext_id},
        )
        assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
class TestPlayerDetailRelatedDataSection:
    """Tests for Related Data section on player detail page."""

    async def test_detail_shows_related_data_links(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """Player detail page shows Related Data section with links."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get(f"/admin/players/{sample_player_id}")
        assert response.status_code == 200
        assert "Related Data" in response.text
        assert f"/admin/players/{sample_player_id}/status" in response.text
        assert f"/admin/players/{sample_player_id}/aliases" in response.text
        assert f"/admin/players/{sample_player_id}/external-ids" in response.text

    async def test_detail_shows_correct_counts(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """Player detail page shows correct counts for aliases and external IDs."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Create some aliases
        alias1 = PlayerAlias(player_id=sample_player_id, full_name="Alias One")
        alias2 = PlayerAlias(player_id=sample_player_id, full_name="Alias Two")
        db_session.add_all([alias1, alias2])

        # Create some external IDs
        ext1 = PlayerExternalId(
            player_id=sample_player_id, system="sys1", external_id="id1"
        )
        db_session.add(ext1)
        await db_session.commit()

        response = await app_client.get(f"/admin/players/{sample_player_id}")
        assert response.status_code == 200
        assert "2 record(s)" in response.text  # aliases
        assert "1 record(s)" in response.text  # external ID

    async def test_detail_shows_status_add_when_none(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """Detail page shows 'Add' for status when none exists."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get(f"/admin/players/{sample_player_id}")
        assert response.status_code == 200
        # Should show "Add" since no status exists
        assert ">Add<" in response.text

    async def test_detail_shows_status_edit_when_exists(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
        sample_player_id: int,
    ):
        """Detail page shows 'Edit' for status when one exists."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        # Create status
        status = PlayerStatus(player_id=sample_player_id, current_team="NYK")
        db_session.add(status)
        await db_session.commit()

        response = await app_client.get(f"/admin/players/{sample_player_id}")
        assert response.status_code == 200
        # Should show "Edit" since status exists
        assert ">Edit<" in response.text
