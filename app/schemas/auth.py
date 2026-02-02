"""Staff auth tables for the admin panel.

These SQLModel tables back the staff-only login system used for `/admin/*` and
for gating staff-only API endpoints.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel


class AuthUser(SQLModel, table=True):  # type: ignore[call-arg]
    """Staff user account."""

    __tablename__ = "auth_users"

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    role: str = Field(index=True)  # "admin" | "worker"
    is_active: bool = Field(default=True, index=True)
    password_hash: str

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    last_login_at: datetime | None = Field(default=None)
    password_changed_at: datetime | None = Field(default=None)
    invited_at: datetime | None = Field(default=None)


class AuthSession(SQLModel, table=True):  # type: ignore[call-arg]
    """Server-side session for a staff user (cookie token is hashed)."""

    __tablename__ = "auth_sessions"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="auth_users.id", index=True)
    token_hash: str = Field(unique=True, index=True)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    expires_at: datetime = Field(index=True)
    revoked_at: datetime | None = Field(default=None, index=True)

    ip: str | None = Field(default=None)
    user_agent: str | None = Field(default=None)
    remember_me: bool = Field(default=False, index=True)


class AuthDatasetPermission(SQLModel, table=True):  # type: ignore[call-arg]
    """Dataset-level permission for a staff user."""

    __tablename__ = "auth_dataset_permissions"

    user_id: int = Field(foreign_key="auth_users.id", primary_key=True)
    dataset: str = Field(primary_key=True)
    can_view: bool = Field(default=False)
    can_edit: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AuthEmailOutbox(SQLModel, table=True):  # type: ignore[call-arg]
    """Outbox-backed email records (password reset uses this during Phase 4)."""

    __tablename__ = "auth_email_outbox"

    id: int | None = Field(default=None, primary_key=True)
    to_email: str = Field(index=True)
    subject: str
    body: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    sent_at: datetime | None = Field(default=None)
    provider: str | None = Field(default=None)


class AuthPasswordResetToken(SQLModel, table=True):  # type: ignore[call-arg]
    """One-time password reset token (store only a hash)."""

    __tablename__ = "auth_password_reset_tokens"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="auth_users.id", index=True)
    token_hash: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    used_at: datetime | None = Field(default=None, index=True)


class AuthInviteToken(SQLModel, table=True):  # type: ignore[call-arg]
    """One-time invitation token for new users (store only a hash)."""

    __tablename__ = "auth_invite_tokens"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="auth_users.id", index=True)
    token_hash: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    used_at: datetime | None = Field(default=None, index=True)
