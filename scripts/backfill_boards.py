#!/usr/bin/env python
"""Phase 2 ingestion harness for the consensus historical backfill.

Consumes the curated candidate list (``docs/consensus_backfill_candidates.json``,
produced by ``curate_backfill_candidates.py``) and runs each historical big
board through the EXISTING extraction pipeline by synthesizing a ``NewsItem``
and calling ``board_extraction_service.extract_board``. That reuses the whole
stack — Substack fetch, Gemini extraction, and the exact→alias→vector
resolution cascade — and lands each board as PENDING for admin review. Nothing
is auto-approved and nothing is guessed: unresolved names stay UNRESOLVED with
candidates, exactly as the live admin trigger produces them.

Idempotency:
  * NewsItems are keyed on ``(source_id, external_id=slug)`` — re-running finds
    the existing item rather than duplicating it.
  * A board is skipped when one already exists for the same source on the same
    calendar date (so boards already in the DB, e.g. the recent May boards, are
    not double-inserted).

The board's ``published_at`` is overridden with the authoritative archive date
(the post's real publish date), not Gemini's in-text guess, so the as-of
history driver sees correct dates.

Usage:
    # list curated candidates with slugs
    ... python scripts/backfill_boards.py --list

    # PROOF: ingest a single board by slug (real Substack + Gemini calls)
    ... python scripts/backfill_boards.py --slug <slug>

    # full run, optionally scoped / capped
    ... python scripts/backfill_boards.py [--source "No Ceilings"] [--limit N]

    # see what would run without touching the network or DB
    ... python scripts/backfill_boards.py --dry-run

Wrap with ``scripts/with-db-env.sh`` so DATABASE_URL + GEMINI creds load.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

CANDIDATES_PATH = "docs/consensus_backfill_candidates.json"


def _load_all_schemas() -> None:
    """Import every app.schemas submodule so cross-table FK metadata resolves.

    Mirrors the discovery in alembic/env.py — importing a single schema (e.g.
    NewsItem) alone leaves its FK to news_sources unresolvable.
    """
    import importlib
    import pkgutil

    import app.schemas as schemas_pkg

    for mod in pkgutil.iter_modules(schemas_pkg.__path__):
        importlib.import_module(f"app.schemas.{mod.name}")


def _load_candidates() -> list[dict]:
    with open(CANDIDATES_PATH) as fh:
        return json.load(fh)["candidates"]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        return None


async def _find_or_create_news_item(db: AsyncSession, cand: dict) -> "object":
    from app.schemas.news_items import NewsItem, NewsItemTag

    slug = cand["slug"]
    sid = cand["source_id"]
    existing = (
        await db.execute(
            select(NewsItem).where(
                NewsItem.source_id == sid,  # type: ignore[arg-type]
                NewsItem.external_id == slug,  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    published_at = _parse_dt(cand["post_date"]) or datetime.utcnow()
    item = NewsItem(
        source_id=sid,
        external_id=slug,
        title=cand["title"][:500],
        url=cand["canonical_url"],
        published_at=published_at,
        tag=NewsItemTag.BIG_BOARD,
    )
    db.add(item)
    await db.flush()
    return item


async def _board_exists_for_date(
    db: AsyncSession, *, source_id: int, when: datetime
) -> bool:
    """True if a big board already exists for this source on this calendar day."""
    from app.schemas.boards import Board, BoardKind

    day_start = datetime(when.year, when.month, when.day)
    day_end = day_start + timedelta(days=1)
    existing = (
        await db.execute(
            select(Board.id)  # type: ignore[call-overload]
            .where(Board.news_source_id == source_id)  # type: ignore[arg-type]
            .where(Board.kind == BoardKind.BIG_BOARD)  # type: ignore[arg-type]
            .where(Board.published_at >= day_start)  # type: ignore[arg-type]
            .where(Board.published_at < day_end)  # type: ignore[arg-type]
            .limit(1)
        )
    ).first()
    return existing is not None


async def _ingest_one(db: AsyncSession, cand: dict) -> tuple[str, str]:
    """Ingest a single candidate. Returns (status, detail)."""
    from app.schemas.boards import BoardEntry
    from app.services.board_extraction_service import (
        BoardExtractionError,
        PaywallDetectedError,
        extract_board,
    )

    post_dt = _parse_dt(cand["post_date"])
    if post_dt is None:
        return ("skipped", "no parseable post_date")

    if await _board_exists_for_date(db, source_id=cand["source_id"], when=post_dt):
        return ("skipped", f"board already exists for {cand['post_date'][:10]}")

    item = await _find_or_create_news_item(db, cand)
    item_id = item.id  # type: ignore[attr-defined]

    try:
        board = await extract_board(db, news_item_id=item_id)
    except PaywallDetectedError as exc:
        await db.rollback()
        return ("paywalled", str(exc)[:120])
    except (BoardExtractionError, NotImplementedError) as exc:
        await db.rollback()
        return ("failed", f"{type(exc).__name__}: {str(exc)[:120]}")

    if board is None:
        await db.commit()
        return ("empty", "extraction produced no entries")

    # The archive date is authoritative; override any Gemini in-text guess.
    if board.published_at != post_dt:
        board.published_at = post_dt
        db.add(board)

    await db.flush()
    entries = (
        (
            await db.execute(
                select(BoardEntry).where(BoardEntry.board_id == board.id)  # type: ignore[arg-type]
            )
        )
        .scalars()
        .all()
    )
    resolved = sum(1 for e in entries if e.player_id is not None)
    unresolved = len(entries) - resolved
    await db.commit()
    return (
        "created",
        f"board_id={board.id} year={board.draft_year} "
        f"entries={len(entries)} resolved={resolved} unresolved={unresolved}",
    )


def _select_candidates(args: argparse.Namespace, cands: list[dict]) -> list[dict]:
    rows = cands
    if args.source:
        rows = [c for c in rows if c["source_name"].lower() == args.source.lower()]
    if args.slug:
        rows = [c for c in rows if c["slug"] == args.slug]
    rows = sorted(rows, key=lambda c: c["post_date"] or "")
    if args.limit is not None:
        rows = rows[: args.limit]
    return rows


async def _run(args: argparse.Namespace) -> None:
    _load_all_schemas()
    cands = _load_candidates()
    selected = _select_candidates(args, cands)

    if args.list or args.dry_run:
        print(f"{len(selected)} candidate(s) selected:")
        for c in selected:
            print(
                f"  [{c['source_name']}] {(c['post_date'] or '')[:10]} "
                f"slug={c['slug']}  {c['title'][:60]}"
            )
        if args.list:
            return
        print("\n(dry run — no network or DB writes)")
        return

    if not selected:
        print("no candidates selected; nothing to do")
        return

    # pool_pre_ping reconnects transparently when the serverless DB (Neon) has
    # dropped an idle connection. A fresh session per candidate plus a broad
    # per-candidate guard means one transient connection reset can't abort the
    # whole run — the failed board is recorded and the next one proceeds (it's
    # idempotent, so a later re-run picks up anything that failed).
    engine = create_async_engine(
        os.environ["DATABASE_URL"], echo=False, pool_pre_ping=True
    )
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    tally: dict[str, int] = {}
    try:
        for c in selected:
            try:
                async with session_factory() as db:
                    status, detail = await _ingest_one(db, c)
            except Exception as exc:  # noqa: BLE001 — keep going on transient errors
                status, detail = (
                    "failed",
                    f"{type(exc).__name__}: {str(exc)[:120]}",
                )
            tally[status] = tally.get(status, 0) + 1
            print(
                f"[{status:>9}] {c['source_name']} "
                f"{(c['post_date'] or '')[:10]} :: {detail}"
            )
    finally:
        await engine.dispose()

    print("\n=== summary ===")
    for k in sorted(tally):
        print(f"  {k}: {tally[k]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", help="limit to one source by exact name")
    ap.add_argument("--slug", help="ingest a single candidate by slug")
    ap.add_argument("--limit", type=int, help="cap number of boards (oldest first)")
    ap.add_argument("--list", action="store_true", help="list candidates and exit")
    ap.add_argument(
        "--dry-run", action="store_true", help="show selection without ingesting"
    )
    args = ap.parse_args()
    if "DATABASE_URL" not in os.environ and not (args.list or args.dry_run):
        print("ERROR: DATABASE_URL not set (wrap with scripts/with-db-env.sh)")
        sys.exit(1)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
