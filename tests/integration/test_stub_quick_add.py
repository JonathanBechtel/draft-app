"""Integration tests for the is_stub checkbox on the create-player form (spec §A2).

Covers:
- POST /admin/players with is_stub=1 persists is_stub=True on the PlayerMaster row.
- POST /admin/players without is_stub persists is_stub=False (default).
- Lifecycle and status rows are created regardless of the stub flag.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_lifecycle import PlayerLifecycle
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from tests.integration.auth_helpers import create_auth_user, login_staff


ADMIN_EMAIL = "stub-qa-admin@example.com"
ADMIN_PASSWORD = "stub-qa-admin-pass-123"


@pytest_asyncio.fixture
async def admin_logged_in(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """Create an admin user and log in on app_client."""
    await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=ADMIN_PASSWORD,
    )
    await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)


def _player_form(
    *,
    display_name: str = "Test Stub Player",
    first_name: str = "Test",
    last_name: str = "Stub",
    is_stub: str | None = None,
) -> dict:
    """Build minimal valid player form data."""
    data: dict = {
        "display_name": display_name,
        "first_name": first_name,
        "last_name": last_name,
    }
    if is_stub is not None:
        data["is_stub"] = is_stub
    return data


@pytest.mark.asyncio
class TestIsStubCheckbox:
    """Tests for is_stub flag persisted through the new-player form."""

    async def test_create_with_stub_flag_sets_is_stub_true(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
    ) -> None:
        """POST with is_stub=1 creates a player with is_stub=True.

        The admin checks the is_stub checkbox, submitting value '1'. The
        created PlayerMaster should have is_stub=True on the DB row.
        """
        _ = admin_logged_in

        resp = await app_client.post(
            "/admin/players",
            data=_player_form(
                display_name="Stub Only Player",
                first_name="Stub",
                last_name="Only",
                is_stub="1",
            ),
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}
        assert "success=created" in resp.headers.get("location", "")

        result = await db_session.execute(
            select(PlayerMaster).where(
                PlayerMaster.display_name == "Stub Only Player"  # type: ignore[arg-type]
            )
        )
        player = result.scalar_one_or_none()
        assert player is not None, "Player was not created"
        assert player.is_stub is True, "Expected is_stub=True when checkbox submitted"

    async def test_create_without_stub_flag_sets_is_stub_false(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
    ) -> None:
        """POST without is_stub creates a player with is_stub=False (default).

        When the checkbox is unchecked, the form field is absent from the POST
        body. The created PlayerMaster should default to is_stub=False.
        """
        _ = admin_logged_in

        resp = await app_client.post(
            "/admin/players",
            data=_player_form(
                display_name="Full Player Record",
                first_name="Full",
                last_name="Player",
                # is_stub absent → unchecked
            ),
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}

        result = await db_session.execute(
            select(PlayerMaster).where(
                PlayerMaster.display_name == "Full Player Record"  # type: ignore[arg-type]
            )
        )
        player = result.scalar_one_or_none()
        assert player is not None, "Player was not created"
        assert player.is_stub is False, "Expected is_stub=False by default"

    async def test_create_stub_also_creates_lifecycle_row(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_logged_in: None,
    ) -> None:
        """When is_stub=1, the create route still creates a lifecycle row.

        The create route always writes lifecycle + status rows; the stub flag
        should not suppress that behaviour.
        """
        _ = admin_logged_in

        resp = await app_client.post(
            "/admin/players",
            data=_player_form(
                display_name="Stub With Lifecycle",
                first_name="Stub",
                last_name="Lifecycle",
                is_stub="1",
            ),
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}

        result = await db_session.execute(
            select(PlayerMaster).where(
                PlayerMaster.display_name == "Stub With Lifecycle"  # type: ignore[arg-type]
            )
        )
        player = result.scalar_one_or_none()
        assert player is not None
        assert player.is_stub is True
        assert player.id is not None

        lc_result = await db_session.execute(
            select(PlayerLifecycle).where(
                PlayerLifecycle.player_id == player.id  # type: ignore[arg-type]
            )
        )
        lc = lc_result.scalar_one_or_none()
        assert lc is not None, "Lifecycle row should exist for stub-flagged player"

        ps_result = await db_session.execute(
            select(PlayerStatus).where(
                PlayerStatus.player_id == player.id  # type: ignore[arg-type]
            )
        )
        ps = ps_result.scalar_one_or_none()
        assert ps is not None, "Status row should exist for stub-flagged player"

    async def test_create_requires_login(self, app_client: AsyncClient) -> None:
        """POST /admin/players redirects to login when not authenticated."""
        resp = await app_client.post(
            "/admin/players",
            data=_player_form(is_stub="1"),
            follow_redirects=False,
        )
        assert resp.status_code in {302, 303}
        assert "/admin/login" in resp.headers.get("location", "")
