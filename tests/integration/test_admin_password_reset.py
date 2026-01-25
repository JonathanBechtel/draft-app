"""Integration tests for staff password reset flow (outbox-backed)."""

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


ADMIN_EMAIL = "admin@example.com"
OLD_PASSWORD = "old-password"
NEW_PASSWORD = "new-password"


@pytest_asyncio.fixture
async def admin_user_id(db_session: AsyncSession) -> int:
    """Create an admin user for password reset tests."""
    return await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=OLD_PASSWORD,
    )


@pytest.mark.asyncio
class TestPasswordReset:
    """Password reset request + confirm behavior."""

    async def test_request_is_generic_and_sends_outbox_for_known_user(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
    ):
        """Requesting a reset always returns generic success; known users get an outbox email."""
        _ = admin_user_id
        response = await app_client.post(
            "/admin/password-reset",
            data={"email": ADMIN_EMAIL},
        )
        assert response.status_code == 200
        assert "if an account exists" in response.text.casefold()

        outbox = await db_session.execute(
            text(
                """
                SELECT subject, body
                FROM auth_email_outbox
                WHERE to_email = :to_email
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"to_email": ADMIN_EMAIL.casefold()},
        )
        subject, body = outbox.one()
        assert "reset" in subject.casefold()
        token = extract_reset_token(body)
        assert token

    async def test_request_is_generic_for_unknown_user_and_sends_nothing(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
    ):
        """Unknown emails still get a generic success response (no user enumeration)."""
        response = await app_client.post(
            "/admin/password-reset",
            data={"email": "nobody@example.com"},
        )
        assert response.status_code == 200
        assert "if an account exists" in response.text.casefold()

        outbox = await db_session.execute(
            text("SELECT COUNT(*) FROM auth_email_outbox WHERE to_email = :to_email"),
            {"to_email": "nobody@example.com"},
        )
        assert outbox.scalar_one() == 0

    async def test_confirm_changes_password_revokes_sessions_and_is_one_time(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
    ):
        """Confirming a reset changes password, revokes sessions, and invalidates the token."""
        _ = admin_user_id

        login = await login_staff(
            app_client,
            email=ADMIN_EMAIL,
            password=OLD_PASSWORD,
        )
        assert login.status_code in {302, 303}

        request_reset = await app_client.post(
            "/admin/password-reset",
            data={"email": ADMIN_EMAIL},
        )
        assert request_reset.status_code == 200

        outbox = await db_session.execute(
            text(
                """
                SELECT body
                FROM auth_email_outbox
                WHERE to_email = :to_email
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"to_email": ADMIN_EMAIL.casefold()},
        )
        token = extract_reset_token(outbox.scalar_one())

        confirm = await app_client.post(
            "/admin/password-reset/confirm",
            data={
                "token": token,
                "password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
            follow_redirects=False,
        )
        assert confirm.status_code in {302, 303}

        still_authed = await app_client.get("/admin", follow_redirects=False)
        assert still_authed.status_code in {302, 303}
        assert still_authed.headers.get("location", "").startswith("/admin/login")

        old_login = await login_staff(
            app_client,
            email=ADMIN_EMAIL,
            password=OLD_PASSWORD,
        )
        assert old_login.status_code == 200
        assert "invalid" in old_login.text.casefold()

        new_login = await login_staff(
            app_client,
            email=ADMIN_EMAIL,
            password=NEW_PASSWORD,
        )
        assert new_login.status_code in {302, 303}

        reuse = await app_client.post(
            "/admin/password-reset/confirm",
            data={"token": token, "password": "another", "confirm_password": "another"},
        )
        assert reuse.status_code == 200  # Returns form with error message

