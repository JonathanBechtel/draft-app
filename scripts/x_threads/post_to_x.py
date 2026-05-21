"""Post a saved draft to X (Twitter).

This is intentionally a no-op stub until X API credentials are configured.
When you wire up posting:

1. ``pip install tweepy`` (or another X v2 client).
2. Add ``X_API_KEY`` / ``X_API_SECRET`` / ``X_ACCESS_TOKEN`` / ``X_ACCESS_SECRET``
   (or OAuth2 user-context credentials) to ``.env``.
3. Replace ``_post_thread`` with real HTTP calls that:
   - Upload images via ``POST /2/media/upload``.
   - Create the lead tweet with ``media_ids``.
   - Reply to ``lead.id`` for each subsequent tweet.
4. On success, flip the row's status to ``posted`` and store the lead
   tweet ID in ``external_post_id``.

Until then, this script exits non-zero so the skill never thinks posting
succeeded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import select  # noqa: E402

from app.schemas.x_post_history import XPostHistory, XPostStatus  # noqa: E402
from app.utils.db_async import SessionLocal  # noqa: E402


REQUIRED_ENV = ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")


def _credentials_present() -> bool:
    return all(os.environ.get(name) for name in REQUIRED_ENV)


def _post_thread(tweets: list[dict[str, Any]], image_paths: list[str]) -> str:
    """Placeholder for the real X API call.

    Returns the external tweet ID of the lead tweet on success.
    """
    raise NotImplementedError(
        "X API posting is not implemented yet. See module docstring."
    )


async def _run(args: argparse.Namespace) -> int:
    if not _credentials_present():
        missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": "missing_credentials",
                    "missing": missing,
                }
            ),
            file=sys.stderr,
        )
        return 4

    async with SessionLocal() as db:
        row: XPostHistory | None
        if args.id is not None:
            stmt = select(XPostHistory).where(XPostHistory.id == args.id)  # type: ignore[arg-type]
        else:
            stmt = (
                select(XPostHistory)
                .where(XPostHistory.draft_dir == str(Path(args.draft_dir).resolve()))  # type: ignore[arg-type]
                .order_by(XPostHistory.created_at.desc())  # type: ignore[attr-defined]
                .limit(1)
            )
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            print("no x_post_history row found", file=sys.stderr)
            return 2
        if row.status == XPostStatus.posted:
            print(
                json.dumps({"status": "already_posted", "id": row.id}),
                file=sys.stderr,
            )
            return 0

        try:
            external_id = _post_thread(row.tweets, row.image_paths)
        except NotImplementedError as exc:
            print(
                json.dumps({"status": "not_implemented", "detail": str(exc)}),
                file=sys.stderr,
            )
            return 5

        async with db.begin():
            from datetime import datetime as _dt

            row.status = XPostStatus.posted
            row.posted_at = _dt.utcnow()
            row.external_post_id = external_id
            db.add(row)

    print(
        json.dumps({"status": "posted", "id": row.id, "external_post_id": external_id})
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--id", type=int, help="x_post_history row ID to post.")
    group.add_argument("--draft-dir", help="Resolve the row from this draft directory.")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
