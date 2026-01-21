"""Integration tests for admin account management."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.auth_helpers import create_auth_user, login_staff


ADMIN_EMAIL = "account-admin@example.com"
ADMIN_PASSWORD = "secure-password-123"


@pytest_asyncio.fixture
async def admin_user_id(db_session: AsyncSession) -> int:
    """Create an admin auth user for account tests."""
    return await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=ADMIN_PASSWORD,
    )


@pytest.mark.asyncio
class TestAccountPage:
    """Tests for the account view page."""

    async def test_account_requires_login(self, app_client: AsyncClient):
        """GET /admin/account redirects to login when not authenticated."""
        response = await app_client.get("/admin/account", follow_redirects=False)
        assert response.status_code in {302, 303}
        assert "/admin/login" in response.headers.get("location", "")

    async def test_account_shows_user_info(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """GET /admin/account shows user email and role when logged in."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/account")
        assert response.status_code == 200
        assert ADMIN_EMAIL in response.text
        assert "Admin" in response.text or "admin" in response.text
        assert "Change Password" in response.text

    async def test_account_shows_success_message(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """GET /admin/account?success=password_changed shows success message."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/account?success=password_changed")
        assert response.status_code == 200
        assert "password" in response.text.lower()
        assert "changed" in response.text.lower() or "success" in response.text.lower()


@pytest.mark.asyncio
class TestPasswordChange:
    """Tests for password change functionality."""

    async def test_change_password_form_requires_login(self, app_client: AsyncClient):
        """GET /admin/account/change-password redirects when not authenticated."""
        response = await app_client.get(
            "/admin/account/change-password", follow_redirects=False
        )
        assert response.status_code in {302, 303}
        assert "/admin/login" in response.headers.get("location", "")

    async def test_change_password_form_renders(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """GET /admin/account/change-password shows the form."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.get("/admin/account/change-password")
        assert response.status_code == 200
        assert 'name="current_password"' in response.text
        assert 'name="new_password"' in response.text
        assert 'name="confirm_password"' in response.text

    async def test_change_password_wrong_current(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """POST with wrong current password shows error."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            "/admin/account/change-password",
            data={
                "current_password": "wrong-password",
                "new_password": "new-secure-password-456",
                "confirm_password": "new-secure-password-456",
            },
        )
        assert response.status_code == 200
        assert "incorrect" in response.text.lower() or "error" in response.text.lower()

    async def test_change_password_mismatch(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """POST with mismatched passwords shows error."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            "/admin/account/change-password",
            data={
                "current_password": ADMIN_PASSWORD,
                "new_password": "new-password-123",
                "confirm_password": "different-password-456",
            },
        )
        assert response.status_code == 200
        assert "match" in response.text.lower() or "do not match" in response.text.lower()

    async def test_change_password_too_short(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """POST with too-short new password shows error."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        response = await app_client.post(
            "/admin/account/change-password",
            data={
                "current_password": ADMIN_PASSWORD,
                "new_password": "short",
                "confirm_password": "short",
            },
        )
        assert response.status_code == 200
        assert "8 character" in response.text.lower() or "error" in response.text.lower()

    async def test_change_password_success(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
    ):
        """Successful password change redirects and updates the database."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        new_password = "new-secure-password-789"
        response = await app_client.post(
            "/admin/account/change-password",
            data={
                "current_password": ADMIN_PASSWORD,
                "new_password": new_password,
                "confirm_password": new_password,
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "success=password_changed" in response.headers.get("location", "")

        # Verify password was changed
        result = await db_session.execute(
            text(
                """
                SELECT password_changed_at
                FROM auth_users
                WHERE id = :user_id
                """
            ),
            {"user_id": admin_user_id},
        )
        password_changed_at = result.scalar_one()
        assert password_changed_at is not None

    async def test_change_password_keeps_current_session(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """After changing password, user remains logged in on current session."""
        _ = admin_user_id
        await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)

        new_password = "new-secure-password-abc"
        await app_client.post(
            "/admin/account/change-password",
            data={
                "current_password": ADMIN_PASSWORD,
                "new_password": new_password,
                "confirm_password": new_password,
            },
            follow_redirects=False,
        )

        # Should still be able to access protected pages
        response = await app_client.get("/admin/account")
        assert response.status_code == 200
        assert ADMIN_EMAIL in response.text
