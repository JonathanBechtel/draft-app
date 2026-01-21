"""Integration tests for admin password reset UI."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.auth_helpers import (
    create_auth_user,
    extract_reset_token,
    login_staff,
)


ADMIN_EMAIL = "reset-ui-admin@example.com"
ADMIN_PASSWORD = "old-password-123"


@pytest_asyncio.fixture
async def admin_user_id(db_session: AsyncSession) -> int:
    """Create an admin auth user for password reset tests."""
    return await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=ADMIN_PASSWORD,
    )


@pytest.mark.asyncio
class TestPasswordResetRequest:
    """Tests for the password reset request form."""

    async def test_reset_request_form_renders(self, app_client: AsyncClient):
        """GET /admin/password-reset shows the request form."""
        response = await app_client.get("/admin/password-reset")
        assert response.status_code == 200
        assert 'name="email"' in response.text
        assert "Reset Password" in response.text or "reset" in response.text.lower()

    async def test_reset_request_shows_confirmation(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """POST /admin/password-reset shows confirmation message."""
        _ = admin_user_id
        response = await app_client.post(
            "/admin/password-reset",
            data={"email": ADMIN_EMAIL},
        )
        assert response.status_code == 200
        assert "check" in response.text.lower() or "email" in response.text.lower()

    async def test_reset_request_unknown_email_same_response(
        self, app_client: AsyncClient
    ):
        """Unknown email shows the same confirmation (no user enumeration)."""
        response = await app_client.post(
            "/admin/password-reset",
            data={"email": "nonexistent@example.com"},
        )
        assert response.status_code == 200
        # Should show the same generic message
        assert "check" in response.text.lower() or "email" in response.text.lower()

    async def test_login_page_links_to_reset(self, app_client: AsyncClient):
        """Login page contains a link to password reset."""
        response = await app_client.get("/admin/login")
        assert response.status_code == 200
        assert "/admin/password-reset" in response.text


@pytest.mark.asyncio
class TestPasswordResetConfirm:
    """Tests for the password reset confirmation form."""

    async def test_confirm_form_requires_token(self, app_client: AsyncClient):
        """GET /admin/password-reset/confirm without token redirects."""
        response = await app_client.get(
            "/admin/password-reset/confirm", follow_redirects=False
        )
        assert response.status_code in {302, 303}
        assert "/admin/password-reset" in response.headers.get("location", "")

    async def test_confirm_form_renders_with_token(self, app_client: AsyncClient):
        """GET /admin/password-reset/confirm with token shows the form."""
        response = await app_client.get(
            "/admin/password-reset/confirm?token=test-token"
        )
        assert response.status_code == 200
        assert 'name="password"' in response.text
        assert 'name="confirm_password"' in response.text
        assert 'name="token"' in response.text

    async def test_confirm_password_mismatch(self, app_client: AsyncClient):
        """POST with mismatched passwords shows error."""
        response = await app_client.post(
            "/admin/password-reset/confirm",
            data={
                "token": "some-token",
                "password": "password-123",
                "confirm_password": "different-456",
            },
        )
        assert response.status_code == 200
        assert "match" in response.text.lower()

    async def test_confirm_password_too_short(self, app_client: AsyncClient):
        """POST with too-short password shows error."""
        response = await app_client.post(
            "/admin/password-reset/confirm",
            data={
                "token": "some-token",
                "password": "short",
                "confirm_password": "short",
            },
        )
        assert response.status_code == 200
        assert "8 character" in response.text.lower()

    async def test_confirm_invalid_token(self, app_client: AsyncClient):
        """POST with invalid token shows error."""
        response = await app_client.post(
            "/admin/password-reset/confirm",
            data={
                "token": "invalid-token-xyz",
                "password": "new-password-123",
                "confirm_password": "new-password-123",
            },
        )
        assert response.status_code == 200
        assert "invalid" in response.text.lower() or "expired" in response.text.lower()

    async def test_confirm_success_flow(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
    ):
        """Full password reset flow: request, confirm, login with new password."""
        _ = admin_user_id

        # Request reset
        await app_client.post(
            "/admin/password-reset",
            data={"email": ADMIN_EMAIL},
        )

        # Get token from outbox
        result = await db_session.execute(
            text(
                """
                SELECT body
                FROM auth_email_outbox
                WHERE to_email = :email
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"email": ADMIN_EMAIL},
        )
        body = result.scalar_one()
        token = extract_reset_token(body)

        # Confirm reset
        new_password = "brand-new-password-999"
        response = await app_client.post(
            "/admin/password-reset/confirm",
            data={
                "token": token,
                "password": new_password,
                "confirm_password": new_password,
            },
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        assert "/admin/password-reset/success" in response.headers.get("location", "")

        # Login with new password should work
        login_response = await login_staff(
            app_client, email=ADMIN_EMAIL, password=new_password
        )
        assert login_response.status_code in {302, 303}
        assert login_response.headers.get("location") == "/admin"


@pytest.mark.asyncio
class TestPasswordResetSuccess:
    """Tests for the password reset success page."""

    async def test_success_page_renders(self, app_client: AsyncClient):
        """GET /admin/password-reset/success shows success message."""
        response = await app_client.get("/admin/password-reset/success")
        assert response.status_code == 200
        assert "success" in response.text.lower() or "complete" in response.text.lower()
        assert "/admin/login" in response.text
