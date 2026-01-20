"""Integration tests for staff auth and session behavior (admin panel)."""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.auth_helpers import create_auth_user, login_staff


ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "correct horse battery staple"


@pytest_asyncio.fixture
async def admin_user_id(db_session: AsyncSession) -> int:
    """Create an admin auth user for tests that need a real login."""
    return await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=ADMIN_PASSWORD,
    )


@pytest.mark.asyncio
class TestAdminAuthUI:
    """Browser-facing auth flows under /admin."""

    async def test_admin_requires_login_redirects(self, app_client: AsyncClient):
        """GET /admin redirects to /admin/login with a next= param when logged out."""
        response = await app_client.get("/admin", follow_redirects=False)
        assert response.status_code in {302, 303}

        location = response.headers.get("location")
        assert location is not None
        split = urlsplit(location)
        assert split.path == "/admin/login"

        params = parse_qs(split.query)
        assert params.get("next") in (["/admin"], ["/admin/"])

    async def test_login_page_renders(self, app_client: AsyncClient):
        """GET /admin/login renders the login form."""
        response = await app_client.get("/admin/login")
        assert response.status_code == 200
        assert 'name="email"' in response.text
        assert 'name="password"' in response.text

    async def test_invalid_login_is_generic(self, app_client: AsyncClient):
        """Invalid credentials return a generic error and do not set a session cookie."""
        response = await login_staff(
            app_client,
            email="nobody@example.com",
            password="wrong",
        )
        assert response.status_code == 200
        assert "invalid" in response.text.casefold()
        assert "set-cookie" not in response.headers

    async def test_valid_login_sets_cookie_and_redirects(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Valid login redirects to /admin and sets a staff session cookie."""
        _ = admin_user_id
        response = await login_staff(
            app_client,
            email="Admin@Example.com",
            password=ADMIN_PASSWORD,
        )
        assert response.status_code in {302, 303}
        assert response.headers.get("location") == "/admin"

        set_cookie = response.headers.get("set-cookie", "")
        assert "dg_admin_session=" in set_cookie
        assert "httponly" in set_cookie.casefold()
        assert "samesite=lax" in set_cookie.casefold()

        admin = await app_client.get("/admin")
        assert admin.status_code == 200

    async def test_open_redirect_is_blocked(
        self,
        app_client: AsyncClient,
        admin_user_id: int,
    ):
        """Login next= only allows local paths (no external redirect)."""
        _ = admin_user_id
        response = await login_staff(
            app_client,
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
            next_path="https://evil.example/phish",
        )
        assert response.status_code in {302, 303}
        location = response.headers.get("location", "")
        assert location == "/admin"
        assert "evil.example" not in location

    async def test_logout_revokes_session_and_clears_cookie(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
    ):
        """POST /admin/logout clears cookies, revokes session, and requires re-login."""
        _ = admin_user_id
        login = await login_staff(
            app_client,
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
        )
        assert login.status_code in {302, 303}

        logout = await app_client.post("/admin/logout", follow_redirects=False)
        assert logout.status_code in {302, 303}
        assert logout.headers.get("location", "").startswith("/admin/login")
        assert "dg_admin_session=" in logout.headers.get("set-cookie", "")

        result = await db_session.execute(
            text(
                """
                SELECT revoked_at
                FROM auth_sessions
                WHERE user_id = :user_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"user_id": admin_user_id},
        )
        revoked_at = result.scalar_one_or_none()
        assert revoked_at is not None

        after = await app_client.get("/admin", follow_redirects=False)
        assert after.status_code in {302, 303}
        assert after.headers.get("location", "").startswith("/admin/login")


@pytest.mark.asyncio
class TestSessionPolicy:
    """Session expiry/idle timeout behavior."""

    async def test_idle_timeout_forces_relogin(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
    ):
        """Stale last_seen_at invalidates the session."""
        _ = admin_user_id
        login = await login_staff(
            app_client,
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
        )
        assert login.status_code in {302, 303}

        stale = datetime.utcnow() - timedelta(days=2)
        await db_session.execute(
            text(
                """
                UPDATE auth_sessions
                SET last_seen_at = :last_seen_at
                WHERE user_id = :user_id AND revoked_at IS NULL
                """
            ),
            {"user_id": admin_user_id, "last_seen_at": stale},
        )
        await db_session.commit()

        response = await app_client.get("/admin", follow_redirects=False)
        assert response.status_code in {302, 303}
        assert response.headers.get("location", "").startswith("/admin/login")

    async def test_remember_me_sets_persistent_cookie_and_longer_expiry(
        self,
        app_client: AsyncClient,
        db_session: AsyncSession,
        admin_user_id: int,
    ):
        """Remember-me should create a longer-lived session with a persistent cookie."""
        _ = admin_user_id

        short = await login_staff(
            app_client,
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
            remember=False,
        )
        assert short.status_code in {302, 303}
        short_cookie = short.headers.get("set-cookie", "").casefold()
        assert "max-age=" not in short_cookie

        short_row = await db_session.execute(
            text(
                """
                SELECT created_at, expires_at
                FROM auth_sessions
                WHERE user_id = :user_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"user_id": admin_user_id},
        )
        short_created_at, short_expires_at = short_row.one()
        assert (short_expires_at - short_created_at) < timedelta(days=2)

        await app_client.post("/admin/logout", follow_redirects=False)

        long = await login_staff(
            app_client,
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
            remember=True,
        )
        assert long.status_code in {302, 303}
        long_cookie = long.headers.get("set-cookie", "").casefold()
        assert "max-age=" in long_cookie

        long_row = await db_session.execute(
            text(
                """
                SELECT created_at, expires_at
                FROM auth_sessions
                WHERE user_id = :user_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"user_id": admin_user_id},
        )
        long_created_at, long_expires_at = long_row.one()
        assert (long_expires_at - long_created_at) > timedelta(days=20)
