"""Shared draft-relevance helpers for content ingestion.

Used by news, podcast, and video ingestion to short-circuit a Gemini
relevance check when a title or description already contains an obvious
draft-related keyword.
"""

from __future__ import annotations

DRAFT_RELEVANCE_KEYWORDS: tuple[str, ...] = (
    "nba draft",
    "mock draft",
    "draft prospect",
    "draft board",
    "draft pick",
    "draft lottery",
    "draft combine",
    "combine",
    "prospect",
    "big board",
    "draft class",
    "draft night",
    "lottery pick",
    "top pick",
    "draft stock",
    "draft order",
)


def check_keyword_relevance(title: str, description: str) -> bool:
    """Return True if title or description contains any draft-related keyword.

    Pure substring match against ``DRAFT_RELEVANCE_KEYWORDS``. Used as the
    first-pass filter before falling back to a Gemini relevance call for
    mixed-topic feeds.

    Args:
        title: Item title.
        description: Item description.

    Returns:
        True if any draft keyword is found in either field.
    """
    text = f"{title} {description}".lower()
    return any(keyword in text for keyword in DRAFT_RELEVANCE_KEYWORDS)
