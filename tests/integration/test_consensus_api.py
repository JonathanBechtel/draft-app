"""Integration tests for the public consensus read API.

Covers:
- GET /api/consensus           (board list)
- GET /api/consensus/player/N  (player detail)
- GET /api/consensus/sources   (source analytics)
- GET /api/consensus/snapshots (snapshot history)

Each group tests: empty year → 200 []; populated year → correct shape /
ordering; unknown player → 404; kind=MOCK_DRAFT → 200 [] / 404.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardEntry, BoardStatus, BoardKind
from app.schemas.consensus import ConsensusTrigger
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from app.services import consensus_service as svc
from tests.integration.conftest import make_player


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_source(db: AsyncSession, name: str) -> NewsSource:
    src = NewsSource(
        name=name,
        display_name=f"{name} Display",
        feed_type=FeedType.RSS,
        feed_url=f"https://example.com/{name}/feed.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db.add(src)
    await db.flush()
    return src


async def _make_approved_board(
    db: AsyncSession,
    *,
    source: NewsSource,
    draft_year: int,
    entries: list[tuple[PlayerMaster, int]],
    hours_ago: float = 24,
) -> Board:
    """Insert a board directly in APPROVED state without triggering hooks."""
    assert source.id is not None
    board = Board(
        news_source_id=source.id,
        news_item_id=None,
        draft_year=draft_year,
        published_at=_now() - timedelta(hours=hours_ago),
        size=len(entries),
        status=BoardStatus.APPROVED,
        approved_at=_now(),
    )
    db.add(board)
    await db.flush()
    assert board.id is not None
    for player, rank in entries:
        assert player.id is not None
        db.add(BoardEntry(board_id=board.id, player_id=player.id, position=rank))
    await db.flush()
    return board


@pytest_asyncio.fixture()
async def two_players(db_session: AsyncSession) -> list[PlayerMaster]:
    """Create two players for use in consensus tests."""
    players = [
        make_player("Cooper", "Flagg", school="Duke"),
        make_player("Dylan", "Harper", school="Rutgers"),
    ]
    for p in players:
        db_session.add(p)
    await db_session.flush()
    return players


@pytest_asyncio.fixture()
async def two_sources(db_session: AsyncSession) -> list[NewsSource]:
    """Create two news sources for use in consensus tests."""
    return [await _make_source(db_session, f"api-src-{i}") for i in range(1, 3)]


@pytest_asyncio.fixture()
async def populated_consensus(
    db_session: AsyncSession,
    two_players: list[PlayerMaster],
    two_sources: list[NewsSource],
) -> None:
    """Seed a recomputed consensus snapshot with two players across two sources."""
    p1, p2 = two_players
    s1, s2 = two_sources

    await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2026,
        entries=[(p1, 1), (p2, 2)],
        hours_ago=48,
    )
    await _make_approved_board(
        db_session,
        source=s2,
        draft_year=2026,
        entries=[(p1, 1), (p2, 2)],
        hours_ago=24,
    )
    await db_session.commit()

    await svc.recompute_consensus(
        db_session, draft_year=2026, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()


# ---------------------------------------------------------------------------
# GET /api/consensus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consensus_board_empty_year_returns_200_empty(
    app_client: AsyncClient,
) -> None:
    """No snapshot exists for an unused year → 200 with empty list."""
    resp = await app_client.get(
        "/api/consensus", params={"draft_year": 2099, "kind": "BIG_BOARD"}
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_consensus_board_returns_rows_ordered_by_rank(
    app_client: AsyncClient,
    populated_consensus: None,
    two_players: list[PlayerMaster],
) -> None:
    """Populated year returns rows ordered by consensus_rank asc."""
    resp = await app_client.get(
        "/api/consensus", params={"draft_year": 2026, "kind": "BIG_BOARD"}
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2
    # Ranks should be strictly ascending.
    ranks = [r["consensus_rank"] for r in rows]
    assert ranks == sorted(ranks)
    assert ranks[0] == 1
    # Both required fields present.
    first = rows[0]
    assert "player_id" in first
    assert "avg_rank" in first
    assert "num_sources" in first


@pytest.mark.asyncio
async def test_consensus_board_row_shape(
    app_client: AsyncClient,
    populated_consensus: None,
) -> None:
    """Response rows contain all documented fields."""
    resp = await app_client.get(
        "/api/consensus", params={"draft_year": 2026, "kind": "BIG_BOARD"}
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) > 0
    required_keys = {
        "player_id",
        "player_name",
        "school",
        "consensus_rank",
        "avg_rank",
        "median_rank",
        "high_rank",
        "low_rank",
        "std_dev",
        "num_sources",
    }
    assert required_keys.issubset(rows[0].keys())


@pytest.mark.asyncio
async def test_consensus_board_mock_draft_returns_empty(
    app_client: AsyncClient,
    populated_consensus: None,
) -> None:
    """kind=MOCK_DRAFT always returns 200 [] (no data yet)."""
    resp = await app_client.get(
        "/api/consensus", params={"draft_year": 2026, "kind": "MOCK_DRAFT"}
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/consensus/player/{player_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_player_consensus_unknown_player_returns_404(
    app_client: AsyncClient,
    populated_consensus: None,
) -> None:
    """A player not on the consensus board → 404."""
    resp = await app_client.get(
        "/api/consensus/player/99999",
        params={"draft_year": 2026, "kind": "BIG_BOARD"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_player_consensus_no_snapshot_returns_404(
    app_client: AsyncClient,
    two_players: list[PlayerMaster],
) -> None:
    """No snapshot at all for the draft year → 404."""
    p1 = two_players[0]
    assert p1.id is not None
    resp = await app_client.get(
        f"/api/consensus/player/{p1.id}",
        params={"draft_year": 2099, "kind": "BIG_BOARD"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_player_consensus_detail_shape_and_source_breakdown(
    app_client: AsyncClient,
    populated_consensus: None,
    two_players: list[PlayerMaster],
    two_sources: list[NewsSource],
) -> None:
    """Detail response includes per-source breakdown and rank history."""
    p1 = two_players[0]
    assert p1.id is not None
    resp = await app_client.get(
        f"/api/consensus/player/{p1.id}",
        params={"draft_year": 2026, "kind": "BIG_BOARD"},
    )
    assert resp.status_code == 200
    body = resp.json()

    # Top-level fields.
    assert body["player_id"] == p1.id
    assert body["consensus_rank"] == 1
    assert body["num_sources"] == 2

    # Per-source breakdown joins through to news_sources.
    source_ranks = body["source_ranks"]
    assert len(source_ranks) == 2
    for sr in source_ranks:
        assert "news_source_id" in sr
        assert "source_name" in sr
        assert "source_display_name" in sr
        assert "source_rank" in sr
        # Both sources ranked p1 #1.
        assert sr["source_rank"] == 1

    # Rank history exists.
    history = body["rank_history"]
    assert len(history) >= 1
    assert "consensus_rank" in history[0]
    assert "computed_at" in history[0]
    assert "snapshot_id" in history[0]


@pytest.mark.asyncio
async def test_player_consensus_mock_draft_returns_404(
    app_client: AsyncClient,
    populated_consensus: None,
    two_players: list[PlayerMaster],
) -> None:
    """kind=MOCK_DRAFT for player detail → 404 (no data yet)."""
    p1 = two_players[0]
    assert p1.id is not None
    resp = await app_client.get(
        f"/api/consensus/player/{p1.id}",
        params={"draft_year": 2026, "kind": "MOCK_DRAFT"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/consensus/sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sources_empty_year_returns_200_empty(
    app_client: AsyncClient,
) -> None:
    """No snapshot for year → 200 []."""
    resp = await app_client.get(
        "/api/consensus/sources",
        params={"draft_year": 2099, "kind": "BIG_BOARD"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_sources_returns_one_row_per_source(
    app_client: AsyncClient,
    populated_consensus: None,
    two_sources: list[NewsSource],
) -> None:
    """One SourceAnalyticsRow per source in the latest snapshot."""
    resp = await app_client.get(
        "/api/consensus/sources",
        params={"draft_year": 2026, "kind": "BIG_BOARD"},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 2

    # Verify the shape.
    required_keys = {
        "id",
        "snapshot_id",
        "news_source_id",
        "source_name",
        "source_display_name",
        "latest_board_id",
        "avg_deviation",
        "contrarian_score",
        "biggest_outlier_player_id",
        "outlier_delta",
    }
    assert required_keys.issubset(rows[0].keys())

    # Source names should reflect our seeded sources (not raw ids).
    returned_source_ids = {r["news_source_id"] for r in rows}
    expected_ids = {s.id for s in two_sources}
    assert returned_source_ids == expected_ids


@pytest.mark.asyncio
async def test_sources_mock_draft_returns_empty(
    app_client: AsyncClient,
    populated_consensus: None,
) -> None:
    """kind=MOCK_DRAFT → 200 []."""
    resp = await app_client.get(
        "/api/consensus/sources",
        params={"draft_year": 2026, "kind": "MOCK_DRAFT"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# GET /api/consensus/snapshots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshots_empty_year_returns_200_empty(
    app_client: AsyncClient,
) -> None:
    """No snapshot for year → 200 []."""
    resp = await app_client.get(
        "/api/consensus/snapshots",
        params={"draft_year": 2099, "kind": "BIG_BOARD"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_snapshots_returns_summaries_newest_first(
    app_client: AsyncClient,
    populated_consensus: None,
) -> None:
    """At least one SnapshotSummary returned, newest first."""
    resp = await app_client.get(
        "/api/consensus/snapshots",
        params={"draft_year": 2026, "kind": "BIG_BOARD"},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) >= 1

    # Shape check.
    required_keys = {"id", "draft_year", "computed_at", "num_boards", "trigger"}
    assert required_keys.issubset(rows[0].keys())
    assert rows[0]["draft_year"] == 2026
    assert rows[0]["num_boards"] == 2

    # Timestamps should be non-increasing (newest first).
    if len(rows) > 1:
        for a, b in zip(rows, rows[1:]):
            assert a["computed_at"] >= b["computed_at"]


@pytest.mark.asyncio
async def test_snapshots_limit_respected(
    app_client: AsyncClient,
    db_session: AsyncSession,
    two_players: list[PlayerMaster],
    two_sources: list[NewsSource],
) -> None:
    """limit parameter caps the number of snapshots returned."""
    p1, p2 = two_players
    s1, s2 = two_sources

    # Seed two separate recomputes so there are at least 2 snapshots.
    await _make_approved_board(
        db_session,
        source=s1,
        draft_year=2027,
        entries=[(p1, 1), (p2, 2)],
    )
    await _make_approved_board(
        db_session,
        source=s2,
        draft_year=2027,
        entries=[(p1, 1), (p2, 2)],
    )
    await db_session.commit()
    await svc.recompute_consensus(
        db_session, draft_year=2027, trigger=ConsensusTrigger.MANUAL
    )
    await svc.recompute_consensus(
        db_session, draft_year=2027, trigger=ConsensusTrigger.SCHEDULED
    )
    await db_session.commit()

    resp = await app_client.get(
        "/api/consensus/snapshots",
        params={"draft_year": 2027, "kind": "BIG_BOARD", "limit": 1},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_snapshots_mock_draft_returns_empty(
    app_client: AsyncClient,
    populated_consensus: None,
) -> None:
    """kind=MOCK_DRAFT → 200 []."""
    resp = await app_client.get(
        "/api/consensus/snapshots",
        params={"draft_year": 2026, "kind": "MOCK_DRAFT"},
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# kind filter: BIG_BOARD rows don't bleed into other query kinds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kind_filter_big_board_data_not_visible_as_mock_draft(
    app_client: AsyncClient,
    populated_consensus: None,
) -> None:
    """BIG_BOARD snapshot data must not surface when kind=MOCK_DRAFT."""
    # Both the board list and sources endpoints.
    for path in ("/api/consensus", "/api/consensus/sources", "/api/consensus/snapshots"):
        resp = await app_client.get(
            path, params={"draft_year": 2026, "kind": "MOCK_DRAFT"}
        )
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"
        assert resp.json() == [], f"{path} should be empty for MOCK_DRAFT"
