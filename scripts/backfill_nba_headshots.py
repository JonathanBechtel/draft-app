r"""Backfill NBA-CDN reference headshots for resolved Summer League players.

Joins ``players_master`` to its ``nba_stats`` external id (seeded by
``scripts/seed_nba_stats_external_ids.py``) and stamps the NBA headshot CDN URL
onto :attr:`PlayerMaster.reference_image_url` for every player that lacks one.
The reference image then flows through the existing stylized-image pipeline
(``scripts/generate_player_images.py``).

By default each candidate URL is validated with a lightweight HTTP request;
players whose CDN headshot 404s (no NBA headshot exists yet) are reported as a
fallback list for manual or college-headshot sourcing. Pass ``--no-validate``
for a fast pass that trusts every URL.

Run C5 (``seed_nba_stats_external_ids.py``) first so the external-id join is
populated.

Usage::

    export DATABASE_URL="postgresql+asyncpg://..."
    conda run -n draftguru --no-capture-output \
        python scripts/backfill_nba_headshots.py

Pass ``--overwrite`` to replace existing reference URLs and ``--dry-run`` to
report counts without writing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Sequence

import httpx
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.services.summer_league.headshots import (
    HeadshotBackfillReport,
    backfill_nba_headshots,
)
from app.utils.db_async import _prepare_asyncpg_connection

load_dotenv()

_VALIDATION_TIMEOUT_SECONDS = 10.0


def _make_http_validator(
    client: httpx.AsyncClient,
):  # -> HeadshotValidator
    """Build a validator that confirms a URL resolves to an image.

    Args:
        client: A shared async HTTP client.

    Returns:
        An async predicate returning ``True`` when the URL responds 200 with an
        ``image/*`` content type, ``False`` otherwise (404, error, non-image).
    """

    async def _validate(url: str) -> bool:
        try:
            response = await client.get(url)
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        content_type = response.headers.get("content-type", "")
        return content_type.startswith("image/")

    return _validate


async def _run(
    *,
    overwrite: bool = False,
    validate: bool = True,
    dry_run: bool = False,
) -> None:
    """Run the headshot backfill sweep.

    Args:
        overwrite: Re-stamp players that already have a reference image.
        validate: Validate each CDN URL with an HTTP request before stamping.
        dry_run: Roll back after reporting the would-be counts.
    """
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    normalized_url, connect_args = _prepare_asyncpg_connection(db_url)
    engine = create_async_engine(normalized_url, connect_args=connect_args)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    report: HeadshotBackfillReport
    async with httpx.AsyncClient(
        timeout=_VALIDATION_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        validator = _make_http_validator(client) if validate else None
        async with session_factory() as session:
            report = await backfill_nba_headshots(
                session, overwrite=overwrite, validator=validator
            )
            if dry_run:
                await session.rollback()
            else:
                await session.commit()

    await engine.dispose()

    action = "Would set" if dry_run else "Set"
    print(
        f"{action}: set={report.set_count}  "
        f"skipped_existing={report.skipped_existing}  "
        f"fallback={len(report.fallback)}",
        flush=True,
    )
    if report.fallback:
        print("Fallback (player_id, person_id) — no CDN headshot, source manually:")
        for player_id, person_id in report.fallback:
            print(f"  player={player_id} person_id={person_id}", flush=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing reference_image_url values",
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip HTTP validation and trust every CDN URL",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report would-be counts without writing to the database",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments; uses ``sys.argv[1:]`` when ``None``.

    Returns:
        Exit code: 0 on success, 1 on error.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    asyncio.run(
        _run(
            overwrite=args.overwrite,
            validate=not args.no_validate,
            dry_run=args.dry_run,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
