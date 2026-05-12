"""Publisher-specific exclusion filters for news ingestion.

Some publishers grant limited reuse rights. These filters honor those
restrictions at the ingestion layer, before any AI relevance scoring
runs -- failing conservatively (when in doubt, exclude). Currently only
Silver Bulletin (natesilver.net) is wired up.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SILVER_BULLETIN_HOST = "natesilver.net"

# Slug substrings that flag a post as a methodology / "how the model works"
# explainer. Joseph George (May 2026) requested we surface the landing /
# rankings pages but not the methodology write-ups he hides on the site.
#
# We deliberately match on slug rather than Substack section_slug because
# section is a poor proxy: ``models-and-forecasts`` contains both the
# rankings pages (admit) and some methodology pages (drop), while ``sports``
# contains the PRISM "how our model works" methodology page itself. Slug
# patterns map directly to article *type*.
_EXCLUDED_SLUG_SUBSTRINGS: tuple[str, ...] = (
    "methodology",
    "model-works",
    "how-we-calculate",
)


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
        return _filter_silver_bulletin(entries)
    return entries, 0


def _filter_silver_bulletin(
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Drop Silver Bulletin methodology / model-works explainers.

    Honors Silver Bulletin's editorial restriction (per Joseph George,
    May 2026 clarification): the rankings/landing pages (e.g., PRISM
    draft rankings) should be displayed; the methodology pages (e.g.,
    "How our PRISM NBA draft model works") should not.

    Filtering is slug-substring only. The Substack ``section_slug`` was
    tried as a secondary signal but proved unreliable (see the comment
    on ``_EXCLUDED_SLUG_SUBSTRINGS``), so it was removed. Non-draft
    dashboard pages (e.g., Trump approval, NCAA team ratings) are
    handled by the downstream draft-relevance gate.
    """
    if not entries:
        return [], 0

    kept: list[dict[str, Any]] = []
    dropped = 0
    for entry in entries:
        url = entry.get("link") or ""
        if not url:
            kept.append(entry)
            continue
        drop, reason = _should_drop_silver_bulletin(url)
        if drop:
            dropped += 1
            logger.info(f"  Silver Bulletin: dropping {url[:80]} ({reason})")
            continue
        kept.append(entry)
    return kept, dropped


def _should_drop_silver_bulletin(url: str) -> tuple[bool, str]:
    """Return ``(drop, reason)`` for a Silver Bulletin entry URL."""
    url_lower = url.lower()
    for marker in _EXCLUDED_SLUG_SUBSTRINGS:
        if marker in url_lower:
            return True, f"slug contains '{marker}'"
    return False, ""
