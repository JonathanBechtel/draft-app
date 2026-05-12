"""Publisher-specific exclusion filters for news ingestion.

Some publishers grant limited reuse rights. These filters honor those
restrictions at the ingestion layer, before any AI relevance scoring
runs — failing conservatively (when in doubt, exclude). Currently only
Silver Bulletin (natesilver.net) is wired up.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SILVER_BULLETIN_HOST = "natesilver.net"
_SUBSTACK_ARCHIVE_URL = "https://www.natesilver.net/api/v1/archive"
_ARCHIVE_PAGE_LIMIT = 50
_ARCHIVE_MAX_POSTS = 500
_ARCHIVE_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
_USER_AGENT = "DraftGuru/1.0 (+https://draftguru)"

_EXCLUDED_SECTION_SLUGS: frozenset[str] = frozenset({"models-and-forecasts"})
_EXCLUDED_SLUG_SUBSTRINGS: tuple[str, ...] = ("methodology",)


async def apply_publisher_filters(
    feed_url: str,
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Dispatch publisher-specific filters based on feed URL host.

    Returns the entries unchanged for any publisher without explicit
    restrictions.

    Args:
        feed_url: The configured ``NewsSource.feed_url``.
        entries: Normalized RSS entries from ``fetch_rss_feed``.

    Returns:
        Tuple of ``(kept_entries, dropped_count)``.
    """
    if _SILVER_BULLETIN_HOST in feed_url.lower():
        return await _filter_silver_bulletin(entries)
    return entries, 0


async def _filter_silver_bulletin(
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Drop methodology posts and Models & Forecasts section posts.

    Honors Silver Bulletin's editorial restriction (per Joseph George,
    May 2026): methodology pages and the Models & Forecasts dashboards
    should not be indexed on DraftGuru.

    Two filters run as a union:
    1. URL slug contains ``methodology`` — catches all known methodology
       articles regardless of section. Cheap, no network dependency.
    2. Substack ``section_slug == "models-and-forecasts"`` — catches the
       live-model dashboard pages. Requires one ``/api/v1/archive`` call
       per ingestion cycle to map URL → section_slug.

    On archive API failure the section filter is skipped (logged as a
    warning); the slug filter still applies.
    """
    if not entries:
        return [], 0

    url_to_section = await _fetch_silver_bulletin_archive()

    kept: list[dict[str, Any]] = []
    dropped = 0
    for entry in entries:
        url = entry.get("link") or ""
        if not url:
            kept.append(entry)
            continue
        drop, reason = _should_drop_silver_bulletin(url, url_to_section)
        if drop:
            dropped += 1
            logger.info(f"  Silver Bulletin: dropping {url[:80]} ({reason})")
            continue
        kept.append(entry)
    return kept, dropped


def _should_drop_silver_bulletin(
    url: str,
    url_to_section: dict[str, str],
) -> tuple[bool, str]:
    """Return ``(drop, reason)`` for a Silver Bulletin entry URL.

    Slug check runs first so it works even when the archive map is empty
    (API failure fallback).
    """
    url_lower = url.lower()
    for marker in _EXCLUDED_SLUG_SUBSTRINGS:
        if marker in url_lower:
            return True, f"slug contains '{marker}'"
    section_slug = url_to_section.get(url)
    if section_slug in _EXCLUDED_SECTION_SLUGS:
        return True, f"section_slug={section_slug}"
    return False, ""


async def _fetch_silver_bulletin_archive() -> dict[str, str]:
    """Build a ``canonical_url -> section_slug`` map from the Substack archive.

    Paginated up to ``_ARCHIVE_MAX_POSTS`` (currently 500). On any failure
    (network, non-2xx, JSON shape change) returns an empty map; callers
    must treat that as "section info unavailable" rather than "no
    exclusions apply".
    """
    url_to_section: dict[str, str] = {}
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": _USER_AGENT},
            timeout=_ARCHIVE_TIMEOUT,
            follow_redirects=True,
        ) as client:
            for offset in range(0, _ARCHIVE_MAX_POSTS, _ARCHIVE_PAGE_LIMIT):
                resp = await client.get(
                    _SUBSTACK_ARCHIVE_URL,
                    params={
                        "sort": "new",
                        "offset": offset,
                        "limit": _ARCHIVE_PAGE_LIMIT,
                    },
                )
                resp.raise_for_status()
                batch = resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                for post in batch:
                    if not isinstance(post, dict):
                        continue
                    canonical_url = post.get("canonical_url")
                    section_slug = post.get("section_slug")
                    if isinstance(canonical_url, str) and isinstance(section_slug, str):
                        url_to_section[canonical_url] = section_slug
                if len(batch) < _ARCHIVE_PAGE_LIMIT:
                    break
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "Silver Bulletin archive fetch failed; falling back to slug-only "
            f"filter: {exc}"
        )
        return {}
    logger.debug(f"Silver Bulletin archive: mapped {len(url_to_section)} URLs")
    return url_to_section
