"""
Homepage service module.

Provides data for homepage sections including market moves,
consensus mock draft, news feed, and specials.
"""

from typing import Any
import random

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.player import get_top_prospects


async def get_market_moves(
    db: AsyncSession,
    limit: int = 10,
) -> list[dict]:
    """
    Get players with biggest rank changes (risers and fallers).

    Currently returns placeholder data. To be replaced with real
    rank change tracking when available.

    Args:
        db: Database session
        limit: Maximum number of movers to return

    Returns:
        List of market move dictionaries with name and change
    """
    # Get real prospects to use as base data
    prospects = await get_top_prospects(db, limit=limit * 2)

    # Generate mock rank changes based on real players
    moves = []
    for i, prospect in enumerate(prospects):
        # Alternate between risers and fallers
        change = random.randint(1, 5) * (1 if i % 2 == 0 else -1)
        moves.append(
            {
                "id": prospect["id"],
                "name": prospect["display_name"],
                "change": change,
                "position": prospect["position"],
                "school": prospect["school"],
            }
        )

    # Sort by absolute change descending
    moves.sort(key=lambda x: abs(x["change"]), reverse=True)
    return moves[:limit]


async def get_consensus_mock_draft(
    db: AsyncSession,
    limit: int = 10,
) -> list[dict]:
    """
    Get consensus mock draft rankings.

    Currently returns placeholder data based on real players.
    To be replaced with aggregated mock draft data when available.

    Args:
        db: Database session
        limit: Number of picks to return

    Returns:
        List of mock draft pick dictionaries
    """
    prospects = await get_top_prospects(db, limit=limit)

    mock_draft = []
    for i, prospect in enumerate(prospects):
        # Generate mock consensus data
        avg_rank = (i + 1) + (random.random() * 2 - 1)  # +/- 1 variance
        change = random.choice([-2, -1, 0, 0, 0, 1, 1, 2])  # Mostly stable

        mock_draft.append(
            {
                "rank": i + 1,
                "player_id": prospect["id"],
                "name": prospect["display_name"],
                "position": prospect["position"],
                "school": prospect["school"],
                "avg_rank": round(avg_rank, 1),
                "change": change,
                "high": max(1, i + 1 - 2),
                "low": i + 1 + 3,
            }
        )

    return mock_draft


def get_news_feed_items(
    limit: int = 10,
    player_filter: str | None = None,
) -> list[dict]:
    """
    Get news feed items for the Live Draft Buzz section.

    Currently returns placeholder data. To be replaced with
    RSS ingestion or API integration when available.

    Args:
        limit: Maximum number of items to return
        player_filter: Optional player name to filter by

    Returns:
        List of news item dictionaries
    """
    # Placeholder news items
    sample_items = [
        {
            "title": "Top Prospect Dominates in Recent Showcase",
            "source": "Draft Insider",
            "source_url": "#",
            "time_ago": "3m",
            "tag": "riser",
        },
        {
            "title": "Combine Results Analysis: Winners and Losers",
            "source": "Hoops Report",
            "source_url": "#",
            "time_ago": "15m",
            "tag": "analysis",
        },
        {
            "title": "Scout's Take: Sleeper Pick to Watch",
            "source": "Draft Central",
            "source_url": "#",
            "time_ago": "1h",
            "tag": "highlight",
        },
        {
            "title": "Injury Update: Top Guard Day-to-Day",
            "source": "Sports Wire",
            "source_url": "#",
            "time_ago": "2h",
            "tag": "faller",
        },
        {
            "title": "Mock Draft 3.0: Updated Big Board",
            "source": "Draft Experts",
            "source_url": "#",
            "time_ago": "3h",
            "tag": "analysis",
        },
        {
            "title": "International Prospect Declares for Draft",
            "source": "Global Hoops",
            "source_url": "#",
            "time_ago": "4h",
            "tag": "riser",
        },
        {
            "title": "Workout Reports: Who Impressed Teams",
            "source": "Draft Central",
            "source_url": "#",
            "time_ago": "6h",
            "tag": "highlight",
        },
        {
            "title": "Team Needs Analysis: What Each Team Seeks",
            "source": "Hoops Report",
            "source_url": "#",
            "time_ago": "8h",
            "tag": "analysis",
        },
    ]

    return sample_items[:limit]


def get_draft_specials(
    limit: int = 5,
) -> list[dict]:
    """
    Get draft position specials for the affiliate section.

    Returns placeholder betting odds data.
    Feature-flagged OFF by default until legal review.

    Args:
        limit: Maximum number of specials to return

    Returns:
        List of special dictionaries with odds
    """
    # Placeholder specials
    specials = [
        {"name": "Cooper Flagg", "position": "#1 Pick", "odds": "-350"},
        {"name": "Dylan Harper", "position": "Top 3", "odds": "-200"},
        {"name": "Ace Bailey", "position": "Top 5", "odds": "-150"},
        {"name": "VJ Edgecombe", "position": "Top 10", "odds": "+120"},
        {"name": "Kasparas Jakucionis", "position": "Lottery", "odds": "+180"},
    ]

    return specials[:limit]


async def get_homepage_data(
    db: AsyncSession,
) -> dict[str, Any]:
    """
    Get all homepage data in a single call.

    Aggregates data from all homepage services for efficient loading.

    Args:
        db: Database session

    Returns:
        Dictionary with all homepage section data
    """
    # Fetch data for all sections
    market_moves = await get_market_moves(db, limit=10)
    mock_draft = await get_consensus_mock_draft(db, limit=10)
    prospects = await get_top_prospects(db, limit=6)
    news_items = get_news_feed_items(limit=8)
    specials = get_draft_specials(limit=5)

    return {
        "market_moves": market_moves,
        "mock_draft": mock_draft,
        "prospects": prospects,
        "news_items": news_items,
        "specials": specials,
    }
