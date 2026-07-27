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


async def main(email: str) -> int:
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
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

    conn = await asyncpg.connect(url)
    try:
        result = await conn.execute(
            "UPDATE auth_users "
            "SET password_hash=$1, password_changed_at=$2, updated_at=$2, is_active=true "
            "WHERE email=$3",
            password_hash,
            now,
            normalized,
        )
    finally:
        await conn.close()

    if result.endswith("0"):
        print(f"No user found with email {normalized}.", file=sys.stderr)
        return 1

    print(f"Password updated for {normalized}.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: reset_admin_password.py <email>", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
