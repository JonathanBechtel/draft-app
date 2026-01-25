"""Staff auth helpers for the admin UI and staff-only APIs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.schemas.auth import (
    AuthEmailOutbox,
    AuthPasswordResetToken,
    AuthSession,
    AuthUser,
)

ADMIN_SESSION_COOKIE_NAME = "dg_admin_session"

SESSION_TTL = timedelta(days=1)
REMEMBER_ME_TTL = timedelta(days=30)
IDLE_TIMEOUT = timedelta(days=1)
LAST_SEEN_UPDATE_THROTTLE = timedelta(minutes=5)
RESET_TOKEN_TTL = timedelta(hours=2)


def normalize_email(email: str) -> str:
    """Normalize an email address for storage and comparisons."""
    return email.strip().casefold()


def _b64decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def hash_pbkdf2_sha256(password: str, *, iterations: int = 210_000) -> str:
    """Return a PBKDF2-SHA256 password hash string."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return f"pbkdf2_sha256${iterations}${_b64encode(salt)}${_b64encode(digest)}"


def verify_pbkdf2_sha256(password: str, encoded_hash: str) -> bool:
    """Verify a PBKDF2-SHA256 password hash compatible with integration tests."""
    try:
        algorithm, iterations_raw, salt_b64, digest_b64 = encoded_hash.split("$", 3)
    except ValueError:
        return False

    if algorithm != "pbkdf2_sha256":
        return False

    try:
        iterations = int(iterations_raw)
    except ValueError:
        return False

    try:
        salt = _b64decode(salt_b64)
        expected = _b64decode(digest_b64)
    except Exception:
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def _hash_token(token: str) -> str:
    key = settings.secret_key.encode("utf-8")
    return hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_session_token() -> str:
    """Generate a raw cookie token (stored only as a hash server-side)."""
    return secrets.token_urlsafe(32)


def generate_password_reset_token() -> str:
    """Generate a raw one-time password reset token."""
    return secrets.token_urlsafe(32)


def _hash_password_reset_token(raw_token: str) -> str:
    return _hash_token(f"reset:{raw_token}")


def sanitize_next_path(next_path: str | None) -> str:
    """Allow only local redirect targets to avoid open redirects."""
    if not next_path:
        return "/admin"
    if not next_path.startswith("/"):
        return "/admin"
    if next_path.startswith("//"):
        return "/admin"
    return next_path


async def authenticate_staff_user(
    db: AsyncSession,
    *,
    email: str,
    password: str,
) -> AuthUser | None:
    """Return an active AuthUser if credentials are valid."""
    normalized_email = normalize_email(email)
    async with db.begin():
        result = await db.execute(
            select(AuthUser).where(
                AuthUser.email == normalized_email,  # type: ignore[arg-type]
                AuthUser.is_active.is_(True),  # type: ignore[attr-defined]
            )
        )
        user = result.scalar_one_or_none()
    if user is None:
        return None
    if not verify_pbkdf2_sha256(password, user.password_hash):
        return None
    return user


async def issue_session(
    db: AsyncSession,
    *,
    user_id: int,
    remember_me: bool,
    ip: str | None,
    user_agent: str | None,
) -> tuple[str, AuthSession]:
    """Create a new session row and return (raw_token, session)."""
    now = datetime.utcnow()
    ttl = REMEMBER_ME_TTL if remember_me else SESSION_TTL
    raw_token = generate_session_token()
    token_hash = _hash_token(raw_token)

    session = AuthSession(
        user_id=user_id,
        token_hash=token_hash,
        created_at=now,
        last_seen_at=now,
        expires_at=now + ttl,
        revoked_at=None,
        ip=ip,
        user_agent=user_agent,
        remember_me=remember_me,
    )

    async with db.begin():
        db.add(session)

    return raw_token, session


async def revoke_session(db: AsyncSession, *, raw_token: str) -> None:
    """Revoke a session token (idempotent)."""
    now = datetime.utcnow()
    token_hash = _hash_token(raw_token)
    async with db.begin():
        await db.execute(
            update(AuthSession)
            .where(
                AuthSession.token_hash == token_hash,  # type: ignore[arg-type]
                AuthSession.revoked_at.is_(None),  # type: ignore[union-attr]
            )
            .values(revoked_at=now)
        )


async def get_user_for_session_token(
    db: AsyncSession,
    *,
    raw_token: str,
) -> AuthUser | None:
    """Return the active user for a valid session token."""
    now = datetime.utcnow()
    token_hash = _hash_token(raw_token)
    async with db.begin():
        result = await db.execute(
            select(AuthSession, AuthUser)
            .join(AuthUser, AuthUser.id == AuthSession.user_id)  # type: ignore[arg-type]
            .where(
                AuthSession.token_hash == token_hash,  # type: ignore[arg-type]
                AuthSession.revoked_at.is_(None),  # type: ignore[union-attr]
                AuthSession.expires_at > now,  # type: ignore[operator,arg-type]
                AuthUser.is_active.is_(True),  # type: ignore[attr-defined]
            )
        )
        row = result.one_or_none()
        if row is None:
            return None

        session, user = row
        if session.last_seen_at < (now - IDLE_TIMEOUT):
            return None

        if now - session.last_seen_at > LAST_SEEN_UPDATE_THROTTLE:
            await db.execute(
                update(AuthSession)
                .where(AuthSession.token_hash == token_hash)  # type: ignore[arg-type]
                .values(last_seen_at=now)
            )

        return user


async def enqueue_password_reset(db: AsyncSession, *, email: str) -> None:
    """Create an outbox email + reset token for a known user.

    This is intentionally a no-op for unknown emails to avoid user enumeration.
    """
    normalized_email = normalize_email(email)
    async with db.begin():
        user_result = await db.execute(
            select(AuthUser).where(
                AuthUser.email == normalized_email,  # type: ignore[arg-type]
                AuthUser.is_active.is_(True),  # type: ignore[attr-defined]
            )
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            return
        if user.id is None:
            return

        now = datetime.utcnow()
        raw_token = generate_password_reset_token()
        token_hash = _hash_password_reset_token(raw_token)

        reset_row = AuthPasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            created_at=now,
            expires_at=now + RESET_TOKEN_TTL,
            used_at=None,
        )
        outbox_row = AuthEmailOutbox(
            to_email=user.email,
            subject="Reset your DraftGuru admin password",
            body=(
                "If you requested a password reset, use this link:\n\n"
                f"/admin/password-reset/confirm?token={raw_token}\n"
            ),
            created_at=now,
            sent_at=None,
            provider=None,
        )

        db.add(reset_row)
        db.add(outbox_row)


async def confirm_password_reset(
    db: AsyncSession,
    *,
    raw_token: str,
    new_password: str,
) -> bool:
    """Apply a password reset and revoke existing sessions.

    Returns True if the token was valid (unused + unexpired) and is now consumed.
    """
    now = datetime.utcnow()
    token_hash = _hash_password_reset_token(raw_token)
    async with db.begin():
        token_result = await db.execute(
            select(AuthPasswordResetToken).where(
                AuthPasswordResetToken.token_hash == token_hash,  # type: ignore[arg-type]
                AuthPasswordResetToken.used_at.is_(None),  # type: ignore[union-attr]
                AuthPasswordResetToken.expires_at > now,  # type: ignore[operator,arg-type]
            )
        )
        token_row = token_result.scalar_one_or_none()
        if token_row is None:
            return False

        user_id = int(token_row.user_id)
        password_hash = hash_pbkdf2_sha256(new_password)

        await db.execute(
            update(AuthUser)
            .where(AuthUser.id == user_id)  # type: ignore[arg-type]
            .values(
                password_hash=password_hash,
                password_changed_at=now,
                updated_at=now,
            )
        )
        await db.execute(
            update(AuthSession)
            .where(
                AuthSession.user_id == user_id,  # type: ignore[arg-type]
                AuthSession.revoked_at.is_(None),  # type: ignore[union-attr]
            )
            .values(revoked_at=now)
        )

        if token_row.id is not None:
            await db.execute(
                update(AuthPasswordResetToken)
                .where(AuthPasswordResetToken.id == token_row.id)  # type: ignore[arg-type]
                .values(used_at=now)
            )
        else:
            await db.execute(
                update(AuthPasswordResetToken)
                .where(
                    AuthPasswordResetToken.token_hash == token_hash  # type: ignore[arg-type]
                )
                .values(used_at=now)
            )

        return True


async def change_password(
    db: AsyncSession,
    *,
    user_id: int,
    current_password: str,
    new_password: str,
    current_session_token_hash: str | None = None,
) -> tuple[bool, str | None]:
    """Change a user's password after verifying the current password.

    Args:
        db: Database session.
        user_id: The ID of the user changing their password.
        current_password: The user's current password (for verification).
        new_password: The new password to set.
        current_session_token_hash: If provided, this session is preserved; others are revoked.

    Returns:
        A tuple of (success, error_message). If success is True, error_message is None.
    """
    async with db.begin():
        # Fetch the user
        user_result = await db.execute(
            select(AuthUser).where(
                AuthUser.id == user_id,  # type: ignore[arg-type]
                AuthUser.is_active.is_(True),  # type: ignore[attr-defined]
            )
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            return False, "User not found."

        # Verify current password
        if not verify_pbkdf2_sha256(current_password, user.password_hash):
            return False, "Current password is incorrect."

        # Validate new password
        if len(new_password) < 8:
            return False, "New password must be at least 8 characters."

        if new_password == current_password:
            return False, "New password must be different from current password."

        now = datetime.utcnow()
        password_hash = hash_pbkdf2_sha256(new_password)

        # Update password
        await db.execute(
            update(AuthUser)
            .where(AuthUser.id == user_id)  # type: ignore[arg-type]
            .values(
                password_hash=password_hash,
                password_changed_at=now,
                updated_at=now,
            )
        )

        # Revoke all other sessions (keep current one if provided)
        if current_session_token_hash:
            await db.execute(
                update(AuthSession)
                .where(
                    AuthSession.user_id == user_id,  # type: ignore[arg-type]
                    AuthSession.revoked_at.is_(None),  # type: ignore[union-attr]
                    AuthSession.token_hash != current_session_token_hash,  # type: ignore[arg-type]
                )
                .values(revoked_at=now)
            )
        else:
            await db.execute(
                update(AuthSession)
                .where(
                    AuthSession.user_id == user_id,  # type: ignore[arg-type]
                    AuthSession.revoked_at.is_(None),  # type: ignore[union-attr]
                )
                .values(revoked_at=now)
            )

        return True, None
