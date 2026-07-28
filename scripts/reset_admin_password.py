r"""Reset a staff user's password on whatever DB ``DATABASE_URL`` points to.

The new password is read from an interactive prompt (never passed as an
argument) so it stays out of shell history and process listings. Useful for
re-syncing a local/dev admin password that has diverged from prod.

Usage:
    scripts/with-db-env.sh conda run -n draftguru \\
        python scripts/reset_admin_password.py <email>
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
from datetime import UTC, datetime

import asyncpg

from app.services.admin_auth_service import hash_pbkdf2_sha256, normalize_email
from app.utils.db_async import _prepare_asyncpg_connection

# `_prepare_asyncpg_connection` returns kwargs for SQLAlchemy's asyncpg dialect;
# only these are accepted by `asyncpg.connect` itself.
_ASYNCPG_CONNECT_KWARGS = {"ssl", "statement_cache_size"}


def _asyncpg_dsn(raw_url: str) -> tuple[str, dict]:
    """Return a raw-asyncpg DSN plus connect kwargs for ``raw_url``.

    Replacing only the driver prefix leaves libpq-only query options such as
    ``channel_binding`` in the DSN, and a Neon-style URL then fails to connect
    before anything is updated. The shared helper strips those and translates
    ``sslmode`` into an ssl context, so reuse it rather than re-deriving.
    """
    normalized_url, connect_args = _prepare_asyncpg_connection(raw_url)
    dsn = normalized_url.replace("postgresql+asyncpg://", "postgresql://")
    kwargs = {k: v for k, v in connect_args.items() if k in _ASYNCPG_CONNECT_KWARGS}
    return dsn, kwargs


async def main(email: str) -> int:
    dsn, connect_kwargs = _asyncpg_dsn(os.environ["DATABASE_URL"])
    normalized = normalize_email(email)

    password = getpass.getpass(f"New password for {normalized}: ")
    if password != getpass.getpass("Confirm password: "):
        print("Passwords did not match; aborting.", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("Password must be at least 8 characters; aborting.", file=sys.stderr)
        return 1

    now = datetime.now(UTC).replace(tzinfo=None)
    password_hash = hash_pbkdf2_sha256(password)

    conn = await asyncpg.connect(dsn, **connect_kwargs)
    try:
        # One transaction: a reset that changed the password but left sessions
        # alive would be worse than not resetting at all. Session auth never
        # compares `password_changed_at`, so an existing cookie stays valid for
        # its full lifetime unless the row is explicitly revoked -- which is what
        # `confirm_password_reset` and `change_password` both do.
        async with conn.transaction():
            user_id = await conn.fetchval(
                "UPDATE auth_users "
                "SET password_hash=$1, password_changed_at=$2, updated_at=$2, "
                "    is_active=true "
                "WHERE email=$3 "
                "RETURNING id",
                password_hash,
                now,
                normalized,
            )
            revoked = 0
            if user_id is not None:
                result = await conn.execute(
                    "UPDATE auth_sessions SET revoked_at=$1 "
                    "WHERE user_id=$2 AND revoked_at IS NULL",
                    now,
                    user_id,
                )
                revoked = int(result.rsplit(" ", 1)[-1])
    finally:
        await conn.close()

    if user_id is None:
        print(f"No user found with email {normalized}.", file=sys.stderr)
        return 1

    print(f"Password updated for {normalized}; revoked {revoked} active session(s).")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: reset_admin_password.py <email>", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
