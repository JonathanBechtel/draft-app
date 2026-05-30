#!/usr/bin/env python
"""Phase 0 sourcing audit for the consensus historical backfill.

Read-only. For every configured ``news_sources`` row, walk the Substack
public archive API (``/api/v1/archive``) across the 2026 draft cycle and
record, per post: title, slug, canonical URL, publish date, paywall
``audience``, and a board-vs-article classification heuristic.

Produces:
  * ``docs/consensus_backfill_manifest.json`` — the full per-post manifest.
  * A printed per-source recoverability summary (free board-like posts,
    date span, paywalled count) so we can see what "weekly over the cycle"
    actually buys before committing ingestion labour or Gemini spend.

Nothing is written to the database. Usage:
    ENV_FILE=/path/.env scripts/with-db-env.sh conda run -n draftguru \
        python scripts/audit_board_archives.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# 2026-cycle floor: boards for the 2026 draft start appearing in fall 2025.
CYCLE_FLOOR = datetime(2025, 9, 1)
ARCHIVE_LIMIT = 50
MAX_PAGES = 40  # safety cap (~2000 posts) against runaway pagination
REQUEST_PAUSE_S = 0.5  # politeness between archive page requests

_HEADERS = {
    "User-Agent": "DraftGuru/1.0 (+https://draftguru.dev; board-audit)",
    "Accept": "application/json",
}
_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)

# Title heuristics — what looks like a rankable board vs. a prose article.
_BIG_BOARD_RE = re.compile(
    r"\b(big board|top\s*\d+|rankings|prospect rankings|draft board|tier(s)?|"
    r"best (available|prospects)|consensus board)\b",
    re.IGNORECASE,
)
_MOCK_RE = re.compile(r"\bmock draft\b", re.IGNORECASE)
MANIFEST_PATH = "docs/consensus_backfill_manifest.json"


@dataclass
class PostRecord:
    source_id: int
    source_name: str
    title: str
    slug: str
    canonical_url: str
    post_date: Optional[str]
    audience: Optional[str]
    is_free: bool
    classification: str  # "big_board" | "mock_draft" | "article"


@dataclass
class SourceResult:
    source_id: int
    source_name: str
    feed_url: str
    archive_host: Optional[str]
    status: str  # "ok" | "not_substack" | "error"
    note: str = ""
    posts: list[PostRecord] = field(default_factory=list)


def _archive_host(feed_url: str) -> Optional[str]:
    """Derive the host to hit the archive API on, from a feed URL."""
    try:
        parsed = urlparse(feed_url)
    except ValueError:
        return None
    if not parsed.hostname:
        return None
    return f"{parsed.scheme or 'https'}://{parsed.hostname}"


def _classify(title: str) -> str:
    if _MOCK_RE.search(title):
        return "mock_draft"
    if _BIG_BOARD_RE.search(title):
        return "big_board"
    return "article"


def _parse_date(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.replace(tzinfo=None)


async def _fetch_archive_page(
    client: httpx.AsyncClient, host: str, offset: int
) -> Optional[list[dict]]:
    """Return one archive page, or None if the host isn't a JSON archive."""
    url = f"{host}/api/v1/archive?sort=new&offset={offset}&limit={ARCHIVE_LIMIT}"
    resp = await client.get(url)
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "json" not in ctype.lower():
        return None
    data = resp.json()
    if not isinstance(data, list):
        return None
    return data


async def audit_source(
    client: httpx.AsyncClient, source_id: int, name: str, feed_url: str
) -> SourceResult:
    host = _archive_host(feed_url)
    result = SourceResult(
        source_id=source_id,
        source_name=name,
        feed_url=feed_url,
        archive_host=host,
        status="ok",
    )
    if host is None:
        result.status = "error"
        result.note = "could not derive host from feed_url"
        return result

    offset = 0
    seen_slugs: set[str] = set()
    try:
        for _page in range(MAX_PAGES):
            page = await _fetch_archive_page(client, host, offset)
            if page is None:
                result.status = "not_substack"
                result.note = "archive endpoint did not return a JSON array"
                return result
            if not page:
                break
            stop = False
            for post in page:
                slug = str(post.get("slug") or "")
                if slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                pdate = _parse_date(post.get("post_date"))
                if pdate is not None and pdate < CYCLE_FLOOR:
                    stop = True
                    continue
                title = str(post.get("title") or "")
                audience = post.get("audience")
                is_free = audience == "everyone" and not post.get(
                    "free_unlock_required"
                )
                result.posts.append(
                    PostRecord(
                        source_id=source_id,
                        source_name=name,
                        title=title,
                        slug=slug,
                        canonical_url=str(post.get("canonical_url") or ""),
                        post_date=pdate.isoformat() if pdate else None,
                        audience=audience if isinstance(audience, str) else None,
                        is_free=bool(is_free),
                        classification=_classify(title),
                    )
                )
            if stop:
                break
            offset += len(page)
            await asyncio.sleep(REQUEST_PAUSE_S)
    except httpx.HTTPError as exc:
        result.status = "error"
        result.note = f"{type(exc).__name__}: {exc}"
        return result
    return result


async def load_sources() -> list[tuple[int, str, str]]:
    eng = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with eng.connect() as c:
            rows = await c.execute(
                text("select id, name, feed_url from news_sources order by id")
            )
            return [(r.id, r.name, r.feed_url) for r in rows]
    finally:
        await eng.dispose()


def _summarize(results: list[SourceResult]) -> None:
    print("\n=== Phase 0 recoverability summary (2026 cycle) ===")
    hdr = (
        f"{'source':<24}{'status':<14}{'boards':>7}{'free':>6}{'mocks':>7}{'span':>26}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in sorted(results, key=lambda x: x.source_name.lower()):
        boards = [p for p in r.posts if p.classification == "big_board"]
        free_boards = [p for p in boards if p.is_free]
        mocks = [p for p in r.posts if p.classification == "mock_draft"]
        dates = [p.post_date for p in free_boards if p.post_date]
        span = ""
        if dates:
            span = f"{min(dates)[:10]} -> {max(dates)[:10]}"
        print(
            f"{r.source_name[:23]:<24}{r.status:<14}"
            f"{len(boards):>7}{len(free_boards):>6}{len(mocks):>7}{span:>26}"
        )


async def main() -> None:
    sources = await load_sources()
    results: list[SourceResult] = []
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True
    ) as client:
        for sid, name, feed_url in sources:
            print(f"auditing {name} ({feed_url}) ...")
            res = await audit_source(client, sid, name, feed_url)
            note = f" [{res.note}]" if res.note else ""
            print(f"  -> {res.status}: {len(res.posts)} cycle posts{note}")
            results.append(res)

    payload = {
        "generated_floor": CYCLE_FLOOR.isoformat(),
        "sources": [
            {
                **{k: v for k, v in asdict(r).items() if k != "posts"},
                "posts": [asdict(p) for p in r.posts],
            }
            for r in results
        ],
    }
    with open(MANIFEST_PATH, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nwrote manifest -> {MANIFEST_PATH}")
    _summarize(results)


if __name__ == "__main__":
    asyncio.run(main())
