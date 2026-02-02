"""Integration-test helpers for staff auth and admin workflows."""

from __future__ import annotations

import base64
import hashlib
import os
import re
from datetime import UTC, datetime

from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PBKDF2_SHA256_PREFIX = "pbkdf2_sha256"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def pbkdf2_sha256_hash(
    password: str,
    *,
    iterations: int = 210_000,
    salt: bytes | None = None,
) -> str:
    """Return a deterministic, portable password hash string.

    Format: "pbkdf2_sha256$<iterations>$<salt_b64>$<digest_b64>"
    """
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return f"{PBKDF2_SHA256_PREFIX}${iterations}${_b64encode(salt)}${_b64encode(dk)}"


async def create_auth_user(
    db_session: AsyncSession,
    *,
    email: str,
    role: str,
    password: str,
    is_active: bool = True,
) -> int:
    """Insert a user row into auth_users and return its id."""
    now = datetime.now(UTC).replace(tzinfo=None)
    password_hash = pbkdf2_sha256_hash(password)
    result = await db_session.execute(
        text(
            """
            INSERT INTO auth_users (
                email,
                role,
                is_active,
                password_hash,
                created_at,
                updated_at
            )
            VALUES (
                :email,
                :role,
                :is_active,
                :password_hash,
                :created_at,
                :updated_at
            )
            RETURNING id
            """
        ),
        {
            "email": email.casefold(),
            "role": role,
            "is_active": is_active,
            "password_hash": password_hash,
            "created_at": now,
            "updated_at": now,
        },
    )
    user_id = result.scalar_one()
    await db_session.commit()
    return int(user_id)


async def grant_dataset_permission(
    db_session: AsyncSession,
    *,
    user_id: int,
    dataset: str,
    can_view: bool,
    can_edit: bool,
) -> None:
    """Grant a dataset permission to a user (upsert)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    await db_session.execute(
        text(
            """
            INSERT INTO auth_dataset_permissions (
                user_id,
                dataset,
                can_view,
                can_edit,
                created_at,
                updated_at
            )
            VALUES (
                :user_id,
                :dataset,
                :can_view,
                :can_edit,
                :created_at,
                :updated_at
            )
            ON CONFLICT (user_id, dataset)
            DO UPDATE SET
                can_view = EXCLUDED.can_view,
                can_edit = EXCLUDED.can_edit,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "user_id": user_id,
            "dataset": dataset,
            "can_view": can_view,
            "can_edit": can_edit,
            "created_at": now,
            "updated_at": now,
        },
    )
    await db_session.commit()


async def login_staff(
    app_client: AsyncClient,
    *,
    email: str,
    password: str,
    remember: bool = False,
    next_path: str | None = None,
) -> Response:
    """Log in via the HTML form and return the response (no redirect follow)."""
    params = {}
    if next_path is not None:
        params["next"] = next_path

    data = {"email": email, "password": password}
    if remember:
        data["remember"] = "1"

    return await app_client.post(
        "/admin/login",
        params=params,
        data=data,
        follow_redirects=False,
    )


def extract_reset_token(email_body: str) -> str:
    """Extract the password reset token from an outbox email body."""
    match = re.search(r"[?&]token=([^&\s]+)", email_body)
    if match is None:
        raise AssertionError("No token=... found in outbox email body")
    return match.group(1)
