"""Thin read-layer for the public consensus API.

Shapes data from ``BigBoardConsensus``, ``SourceAnalytics``, and
``ConsensusSnapshot`` into the Pydantic response models consumed by
``app/routes/consensus.py``.

The write/compute path lives in ``consensus_service.py``; this module
only reads. UI tickets #218–#221 will extend the helpers here.
"""

from __future__ import annotations

import statistics
from datetime import date
from collections.abc import Iterable
from typing import Any, Optional, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consensus import (
    ConsensusRow,
    MockConsensusRow,
    PlayerConsensusDetail,
    RankHistoryPoint,
    SnapshotSummary,
    SourceAnalyticsRow,
    SourceRankEntry,
)
from app.schemas.boards import Board, BoardEntry, BoardStatus
from app.schemas.nba_teams import NbaTeam
from app.schemas.consensus import (
    BigBoardConsensus,
    ConsensusSnapshot,
    SourceAnalytics,
)
from app.schemas.news_items import NewsItem
from app.schemas.news_sources import NewsSource
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.positions import Position
from app.services.image_assets_service import get_current_image_urls_for_players
from app.services.school_logo_service import get_logo_urls_for_schools
from app.utils.combine_formatters import format_height_inches

# Style used for consensus-board thumbnails. Matches the default style the
# trending and stats lists already use, so we hit the same generated assets.
_CONSENSUS_PHOTO_STYLE = "default"

# The homepage composes several consensus panels in one request. Opt-in
# request-scoped caches let those panels reuse the same latest snapshot and
# source analytics without changing the standalone service semantics used by
# API routes and background jobs.
_CONSENSUS_SNAPSHOT_CACHE_KEY = "_draftguru_consensus_snapshot_ids"
_CONSENSUS_SOURCE_ANALYTICS_CACHE_KEY = "_draftguru_consensus_source_analytics"
_CONSENSUS_SNAPSHOT_OBJECT_CACHE_KEY = "_draftguru_consensus_snapshots"
_CONSENSUS_BOARD_CACHE_KEY = "_draftguru_consensus_boards"
_CONSENSUS_ENTRY_CACHE_KEY = "_draftguru_consensus_board_entries"
_CONSENSUS_SOURCE_CACHE_KEY = "_draftguru_consensus_sources"


def enable_consensus_request_cache(db: AsyncSession) -> None:
    """Enable request-scoped consensus read caches for a composed page."""
    db.info[_CONSENSUS_SNAPSHOT_CACHE_KEY] = {}
    db.info[_CONSENSUS_SOURCE_ANALYTICS_CACHE_KEY] = {}
    db.info[_CONSENSUS_SNAPSHOT_OBJECT_CACHE_KEY] = {}
    db.info[_CONSENSUS_BOARD_CACHE_KEY] = {}
    db.info[_CONSENSUS_ENTRY_CACHE_KEY] = {}
    db.info[_CONSENSUS_SOURCE_CACHE_KEY] = {}


async def _resolve_snapshot_id(
    db: AsyncSession,
    *,
    draft_year: int,
    snapshot_id: Optional[int],
) -> Optional[int]:
    """Return the target snapshot id, scoped to ``draft_year``.

    When ``snapshot_id`` is supplied it is validated against ``draft_year``
    in the same query — a snapshot belonging to a different year returns
    ``None`` so callers behave identically to "no data" rather than
    surfacing cross-year rows under the requested year. When ``snapshot_id``
    is omitted, the most recent snapshot for ``draft_year`` is selected.
    """
    cache = db.info.get(_CONSENSUS_SNAPSHOT_CACHE_KEY)
    cache_key = (draft_year, snapshot_id)
    if isinstance(cache, dict) and cache_key in cache:
        return cache[cache_key]

    if snapshot_id is not None:
        resolved = await db.scalar(
            select(ConsensusSnapshot.id)  # type: ignore[call-overload]
            .where(ConsensusSnapshot.id == snapshot_id)  # type: ignore[arg-type]
            .where(ConsensusSnapshot.draft_year == draft_year)  # type: ignore[arg-type]
        )
    else:
        resolved = await db.scalar(
            select(ConsensusSnapshot.id)  # type: ignore[call-overload]
            .where(ConsensusSnapshot.draft_year == draft_year)  # type: ignore[arg-type]
            .order_by(ConsensusSnapshot.computed_at.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
    if isinstance(cache, dict):
        cache[cache_key] = resolved
    return resolved  # type: ignore[return-value]


async def _get_source_analytics_rows(
    db: AsyncSession, snapshot_id: int
) -> list[SourceAnalytics]:
    """Load source analytics once when a composed homepage needs two panels."""
    cache = db.info.get(_CONSENSUS_SOURCE_ANALYTICS_CACHE_KEY)
    if isinstance(cache, dict) and snapshot_id in cache:
        return list(cache[snapshot_id])

    rows = list(
        (
            await db.execute(
                select(SourceAnalytics).where(  # type: ignore[call-overload]
                    SourceAnalytics.snapshot_id == snapshot_id  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    if isinstance(cache, dict):
        cache[snapshot_id] = rows
    return rows


async def _load_consensus_snapshot(
    db: AsyncSession, snapshot_id: int
) -> Optional[ConsensusSnapshot]:
    """Load and cache one snapshot object for the composed homepage."""
    cache = db.info.get(_CONSENSUS_SNAPSHOT_OBJECT_CACHE_KEY)
    if isinstance(cache, dict) and snapshot_id in cache:
        return cache[snapshot_id]
    snapshot = (
        await db.execute(
            select(ConsensusSnapshot).where(  # type: ignore[call-overload]
                ConsensusSnapshot.id == snapshot_id  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()
    if isinstance(cache, dict):
        cache[snapshot_id] = snapshot
    return snapshot


async def _load_consensus_boards(
    db: AsyncSession,
    board_ids: list[int],
) -> list[Board]:
    """Load and cache board rows shared by spotlight and freshness."""
    if not board_ids:
        return []
    cache = db.info.get(_CONSENSUS_BOARD_CACHE_KEY)
    if isinstance(cache, dict) and all(board_id in cache for board_id in board_ids):
        return [
            cache[board_id] for board_id in board_ids if cache[board_id] is not None
        ]
    rows = list(
        (
            await db.execute(
                select(Board).where(  # type: ignore[call-overload]
                    Board.id.in_(board_ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    if isinstance(cache, dict):
        for row in rows:
            if row.id is not None:
                cache[row.id] = row
        for board_id in board_ids:
            cache.setdefault(board_id, None)
    return rows


async def _load_consensus_entries(
    db: AsyncSession,
    board_ids: list[int],
) -> list[BoardEntry]:
    """Load and cache board entries shared by controversy and spotlight."""
    if not board_ids:
        return []
    cache = db.info.get(_CONSENSUS_ENTRY_CACHE_KEY)
    if isinstance(cache, dict) and all(board_id in cache for board_id in board_ids):
        return [entry for board_id in board_ids for entry in cache[board_id]]
    rows = list(
        (
            await db.execute(
                select(BoardEntry).where(  # type: ignore[call-overload, attr-defined]
                    cast(Any, BoardEntry.board_id).in_(board_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    if isinstance(cache, dict):
        for board_id in board_ids:
            cache[board_id] = [row for row in rows if row.board_id == board_id]
    return rows


async def _load_consensus_sources(
    db: AsyncSession,
    source_ids: list[int],
) -> list[NewsSource]:
    """Load and cache source rows shared by attribution panels."""
    if not source_ids:
        return []
    cache = db.info.get(_CONSENSUS_SOURCE_CACHE_KEY)
    if isinstance(cache, dict) and all(source_id in cache for source_id in source_ids):
        return [
            cache[source_id] for source_id in source_ids if cache[source_id] is not None
        ]
    rows = list(
        (
            await db.execute(
                select(NewsSource).where(  # type: ignore[call-overload]
                    NewsSource.id.in_(source_ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    if isinstance(cache, dict):
        for row in rows:
            if row.id is not None:
                cache[row.id] = row
        for source_id in source_ids:
            cache.setdefault(source_id, None)
    return rows


async def _player_name_map(
    db: AsyncSession, player_ids: list[int]
) -> dict[int, PlayerMaster]:
    """Return a ``player_id -> PlayerMaster`` map for a batch of ids."""
    if not player_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(PlayerMaster).where(  # type: ignore[call-overload]
                    PlayerMaster.id.in_(player_ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    return {p.id: p for p in rows if p.id is not None}


def _years_old(birth: Optional[date], as_of: Optional[date] = None) -> Optional[float]:
    """Return the player's age in years (one-decimal precision) from a birthdate.

    ``None`` when ``birth`` is missing. Uses today's date by default — the
    consensus board surfaces age as informational context, not a stat that
    needs anchoring to draft day.
    """
    if birth is None:
        return None
    today = as_of or date.today()
    return round((today - birth).days / 365.25, 1)


async def _recent_ranks_map(
    db: AsyncSession,
    *,
    draft_year: int,
    player_ids: list[int],
    up_to_snapshot_id: Optional[int] = None,
    limit_per_player: int = 8,
) -> dict[int, list[int]]:
    """Return ``{player_id: [rank, ...]}`` of each player's recent ranks.

    The list is ordered oldest-first across the last ``limit_per_player``
    snapshots for ``draft_year``. Powers the per-row sparkline on the
    consensus board. One query, grouped + sliced in Python.

    Args:
        db: Async DB session.
        draft_year: Year scope.
        player_ids: Players to fetch history for.
        up_to_snapshot_id: When set, cap history at that snapshot's
            ``computed_at`` (inclusive). Required for point-in-time
            historical responses — a request for an older
            ``snapshot_id`` must not surface ranks from snapshots that
            were computed AFTER it. ``None`` means "no upper bound"
            (the caller wants the full latest series).
        limit_per_player: Max series length per player.
    """
    if not player_ids:
        return {}
    stmt = (
        select(  # type: ignore[call-overload]
            BigBoardConsensus.player_id,
            BigBoardConsensus.consensus_rank,
            ConsensusSnapshot.computed_at,
        )
        .join(
            ConsensusSnapshot,
            ConsensusSnapshot.id == BigBoardConsensus.snapshot_id,  # type: ignore[arg-type]
        )
        .where(BigBoardConsensus.draft_year == draft_year)  # type: ignore[arg-type]
        .where(
            BigBoardConsensus.player_id.in_(player_ids)  # type: ignore[attr-defined]
        )
        .order_by(
            BigBoardConsensus.player_id,  # type: ignore[arg-type]
            ConsensusSnapshot.computed_at,  # type: ignore[arg-type]
        )
    )
    if up_to_snapshot_id is not None:
        # Scalar subquery: only include snapshots whose computed_at is at
        # or before the target snapshot's — so a historical query stays
        # point-in-time and doesn't leak future ranks.
        cutoff_subq = (
            select(ConsensusSnapshot.computed_at)  # type: ignore[call-overload]
            .where(ConsensusSnapshot.id == up_to_snapshot_id)  # type: ignore[arg-type]
            .scalar_subquery()
        )
        stmt = stmt.where(ConsensusSnapshot.computed_at <= cutoff_subq)  # type: ignore[arg-type]
    rows = (await db.execute(stmt)).all()
    series: dict[int, list[int]] = {}
    for pid, rank, _computed_at in rows:
        series.setdefault(pid, []).append(rank)
    # Cap the trailing window per player (sparkline shouldn't grow unbounded).
    return {pid: ranks[-limit_per_player:] for pid, ranks in series.items()}


def _format_position(code: Optional[str], raw: Optional[str]) -> Optional[str]:
    """Return a short, display-ready position label.

    Prefers the structured position ``code`` (e.g. ``pg_sg``/``pg-sg`` ->
    ``PG/SG``, ``c`` -> ``C``); falls back to abbreviating the free-text
    ``raw_position`` (e.g. ``Guard`` -> ``G``). Returns ``None`` when neither
    is known.
    """
    if code:
        # Hybrid codes appear with either separator in the data (pg_sg, pg-sg).
        return code.upper().replace("_", "/").replace("-", "/")
    if raw:
        word = raw.strip()
        return {"guard": "G", "forward": "F", "center": "C"}.get(word.lower(), word)
    return None


async def _player_status_map(
    db: AsyncSession, player_ids: list[int]
) -> dict[int, dict[str, Any]]:
    """Return ``player_id -> {position, height, weight}`` from ``PlayerStatus``.

    Best-effort physical profile for the consensus board. Mirrors the source
    used by the trending and player-detail surfaces so the board agrees with
    them. Players without a status row are simply absent from the map.
    """
    if not player_ids:
        return {}
    stmt = (
        select(  # type: ignore[call-overload]
            PlayerStatus.player_id,
            PlayerStatus.raw_position,
            PlayerStatus.height_in,
            PlayerStatus.weight_lb,
            cast(Any, Position.code).label("position_code"),
        )
        .select_from(PlayerStatus)
        .outerjoin(Position, cast(Any, Position.id) == PlayerStatus.position_id)
        .where(cast(Any, PlayerStatus.player_id).in_(player_ids))
    )
    out: dict[int, dict[str, Any]] = {}
    for row in (await db.execute(stmt)).mappings().all():
        pid = row["player_id"]
        if pid is None:
            continue
        out[int(pid)] = {
            "position": _format_position(row["position_code"], row["raw_position"]),
            "height": format_height_inches(row["height_in"]),
            "weight": row["weight_lb"],
        }
    return out


def _to_consensus_row(
    bbc: BigBoardConsensus,
    player: Optional[PlayerMaster],
    school_logo_url: Optional[str] = None,
    photo_url: Optional[str] = None,
    recent_ranks: Optional[list[int]] = None,
    status: Optional[dict[str, Any]] = None,
) -> ConsensusRow:
    """Map a ``BigBoardConsensus`` ORM row to the ``ConsensusRow`` model.

    Args:
        bbc: The ORM consensus row.
        player: The resolved ``PlayerMaster`` for ``bbc.player_id`` if any.
        school_logo_url: Pre-resolved school logo URL (async/DB-cached, so
            the caller batch-resolves and passes it in).
        photo_url: Pre-resolved player photo URL from ``PlayerImageAsset``
            (caller batch-resolves via ``get_current_image_urls_for_players``).
            ``None`` for players without a generated image — templates skip
            the ``<img>`` rather than show a placeholder.
        recent_ranks: Oldest-to-newest series of this player's consensus
            ranks across recent snapshots (caller batch-resolves via
            ``_recent_ranks_map``); rendered as a sparkline.
        status: Pre-resolved ``{position, height, weight}`` for this player
            (caller batch-resolves via ``_player_status_map``); all keys
            optional and absent for players without a status row.
    """
    status = status or {}
    return ConsensusRow(
        player_id=bbc.player_id,
        player_name=player.display_name if player else None,
        school=player.school if player else None,
        slug=player.slug if player else None,
        photo_url=photo_url,
        school_logo_url=school_logo_url,
        age=_years_old(player.birthdate) if player else None,
        position=status.get("position"),
        height=status.get("height"),
        weight=status.get("weight"),
        consensus_rank=bbc.consensus_rank,
        avg_rank=bbc.avg_rank,
        median_rank=bbc.median_rank,
        high_rank=bbc.high_rank,
        low_rank=bbc.low_rank,
        std_dev=bbc.std_dev,
        num_sources=bbc.num_sources,
        prev_rank=bbc.prev_rank,
        rank_delta=bbc.rank_delta,
        recent_ranks=recent_ranks or [],
    )


async def get_consensus_board(
    db: AsyncSession,
    *,
    draft_year: int,
    snapshot_id: Optional[int] = None,
) -> list[ConsensusRow]:
    """Return ordered consensus rows for a draft year.

    Args:
        db: Async DB session.
        draft_year: The draft class to query.
        snapshot_id: Specific snapshot; defaults to the most recent.

    Returns:
        Rows ordered by ``consensus_rank`` asc. Empty list when no
        snapshot exists for the year.
    """
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=snapshot_id)
    if sid is None:
        return []

    bbc_rows = (
        (
            await db.execute(
                select(BigBoardConsensus)  # type: ignore[call-overload]
                .where(BigBoardConsensus.snapshot_id == sid)  # type: ignore[arg-type]
                .order_by(BigBoardConsensus.consensus_rank)  # type: ignore[arg-type]
            )
        )
        .scalars()
        .all()
    )

    if not bbc_rows:
        return []

    player_map = await _player_name_map(db, [r.player_id for r in bbc_rows])
    # Batch-resolve school logos (cache-backed), player photos (from
    # PlayerImageAsset), and per-player rank history once per snapshot so
    # each row lookup is a plain dict get instead of N async calls.
    logo_map = await get_logo_urls_for_schools(
        db,
        [player_map[r.player_id].school for r in bbc_rows if r.player_id in player_map],
    )
    photo_map = await get_current_image_urls_for_players(
        db,
        player_ids=[r.player_id for r in bbc_rows],
        style=_CONSENSUS_PHOTO_STYLE,
    )
    history_map = await _recent_ranks_map(
        db,
        draft_year=draft_year,
        player_ids=[r.player_id for r in bbc_rows],
        # Cap history at the requested snapshot so a historical query
        # (older ``snapshot_id``) stays point-in-time. ``sid`` is the
        # resolved snapshot id — when the caller asked for the latest,
        # passing the latest's id is equivalent to no upper bound.
        up_to_snapshot_id=sid,
    )
    status_map = await _player_status_map(db, [r.player_id for r in bbc_rows])
    return [
        _to_consensus_row(
            r,
            player_map.get(r.player_id),
            school_logo_url=(
                logo_map.get(player_map[r.player_id].school or "")
                if r.player_id in player_map
                else None
            ),
            photo_url=photo_map.get(r.player_id),
            recent_ranks=history_map.get(r.player_id, []),
            status=status_map.get(r.player_id),
        )
        for r in bbc_rows
    ]


async def get_mock_consensus_board(
    db: AsyncSession,
    *,
    draft_year: int,
    snapshot_id: Optional[int] = None,
) -> list[MockConsensusRow]:
    """Return the consensus board with each row's draft-slot team overlaid.

    The ranking is the unified consensus (``get_consensus_board``) unchanged.
    Each player at ``consensus_rank`` N is mapped onto overall pick N of the
    draft-order reference, so the post-lottery view reads as a mock draft:
    "team that owns pick N is projected to take the consensus-N player".

    Rows whose ``consensus_rank`` has no matching pick slot (consensus runs
    deeper than the seeded order, or the order isn't seeded) keep all team
    fields ``None`` — the template omits the team chip for them.

    Args:
        db: Async DB session.
        draft_year: The draft class to query.
        snapshot_id: Specific snapshot; defaults to the most recent.

    Returns:
        Rows ordered by ``consensus_rank`` asc. Empty list when no snapshot
        exists for the year.
    """
    from app.services.draft_order_service import get_draft_order

    base_rows = await get_consensus_board(
        db, draft_year=draft_year, snapshot_id=snapshot_id
    )
    if not base_rows:
        return []

    slots = await get_draft_order(db, draft_year=draft_year)
    slot_by_pick = {s.overall_pick: s for s in slots}

    # Batch-resolve every team referenced by the order (owners + original
    # owners) in one query.
    team_ids: set[int] = set()
    for s in slots:
        team_ids.add(s.team_id)
        if s.original_team_id is not None:
            team_ids.add(s.original_team_id)
    team_map: dict[int, NbaTeam] = {}
    if team_ids:
        team_rows = (
            (
                await db.execute(
                    select(NbaTeam).where(  # type: ignore[call-overload]
                        NbaTeam.id.in_(team_ids)  # type: ignore[union-attr]
                    )
                )
            )
            .scalars()
            .all()
        )
        team_map = {t.id: t for t in team_rows if t.id is not None}

    out: list[MockConsensusRow] = []
    for row in base_rows:
        data = row.model_dump()
        slot = slot_by_pick.get(row.consensus_rank)
        team = team_map.get(slot.team_id) if slot is not None else None
        if slot is not None and team is not None:
            original = (
                team_map.get(slot.original_team_id)
                if slot.original_team_id is not None
                else None
            )
            data.update(
                overall_pick=slot.overall_pick,
                round=slot.round,
                team_name=team.name,
                team_abbreviation=team.abbreviation,
                team_slug=team.slug,
                team_logo_url=team.logo_url,
                team_primary_color=team.primary_color,
                original_team_abbreviation=(
                    original.abbreviation if original is not None else None
                ),
                trade_note=slot.trade_note,
            )
        out.append(MockConsensusRow(**data))
    return out


async def get_player_consensus_detail(
    db: AsyncSession,
    *,
    player_id: int,
    draft_year: int,
) -> Optional[PlayerConsensusDetail]:
    """Return full consensus detail for one player.

    Args:
        db: Async DB session.
        player_id: The player to look up.
        draft_year: Draft class to query.

    Returns:
        ``PlayerConsensusDetail`` when a current consensus row exists,
        ``None`` otherwise (caller should raise 404).
    """
    # Current snapshot row.
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return None

    bbc = (
        await db.execute(
            select(BigBoardConsensus)  # type: ignore[call-overload]
            .where(BigBoardConsensus.snapshot_id == sid)  # type: ignore[arg-type]
            .where(BigBoardConsensus.player_id == player_id)  # type: ignore[arg-type]
        )
    ).scalar_one_or_none()

    if bbc is None:
        return None

    # Player metadata.
    player = (
        await db.execute(
            select(PlayerMaster).where(  # type: ignore[call-overload]
                PlayerMaster.id == player_id  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()

    # Per-source breakdown.
    # Join board_entries → boards → news_sources for the boards that fed
    # the latest snapshot.
    snapshot = await _load_consensus_snapshot(db, sid)

    source_ranks: list[SourceRankEntry] = []
    if snapshot and snapshot.board_ids:
        entry_rows = (
            await db.execute(
                select(BoardEntry.board_id, BoardEntry.position)  # type: ignore[call-overload]
                .where(BoardEntry.board_id.in_(snapshot.board_ids))  # type: ignore[union-attr, attr-defined]
                .where(BoardEntry.player_id == player_id)  # type: ignore[arg-type]
            )
        ).all()

        if entry_rows:
            bid_to_rank = {row.board_id: row.position for row in entry_rows}
            board_rows = (
                (
                    await db.execute(
                        select(Board).where(  # type: ignore[call-overload]
                            Board.id.in_(list(bid_to_rank.keys()))  # type: ignore[union-attr]
                        )
                    )
                )
                .scalars()
                .all()
            )

            source_ids = [b.news_source_id for b in board_rows]
            source_rows = (
                (
                    await db.execute(
                        select(NewsSource).where(  # type: ignore[call-overload]
                            NewsSource.id.in_(source_ids)  # type: ignore[union-attr]
                        )
                    )
                )
                .scalars()
                .all()
            )
            source_map = {s.id: s for s in source_rows if s.id is not None}

            # Resolve the source article (mock/board) each board was extracted
            # from so the per-source breakdown can link out to it. Synthetic /
            # legacy boards carry no ``news_item_id`` and stay unlinked.
            news_item_ids = [
                b.news_item_id for b in board_rows if b.news_item_id is not None
            ]
            article_map: dict[int, NewsItem] = {}
            if news_item_ids:
                article_rows = (
                    (
                        await db.execute(
                            select(NewsItem).where(  # type: ignore[call-overload]
                                NewsItem.id.in_(news_item_ids)  # type: ignore[union-attr]
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                article_map = {a.id: a for a in article_rows if a.id is not None}

            for board in board_rows:
                if board.id is None:
                    continue
                src = source_map.get(board.news_source_id)
                article = (
                    article_map.get(board.news_item_id)
                    if board.news_item_id is not None
                    else None
                )
                source_ranks.append(
                    SourceRankEntry(
                        news_source_id=board.news_source_id,
                        source_name=src.name
                        if src
                        else f"source_{board.news_source_id}",
                        source_display_name=src.display_name
                        if src
                        else f"source_{board.news_source_id}",
                        source_rank=bid_to_rank[board.id],
                        article_url=article.url if article else None,
                        article_title=article.title if article else None,
                    )
                )
        source_ranks.sort(key=lambda e: e.source_rank)

    # Rank history (oldest → newest).
    history_bbc_rows = (
        await db.execute(
            select(BigBoardConsensus, ConsensusSnapshot.computed_at)  # type: ignore[call-overload]
            .join(
                ConsensusSnapshot,
                ConsensusSnapshot.id == BigBoardConsensus.snapshot_id,  # type: ignore[arg-type]
            )
            .where(BigBoardConsensus.player_id == player_id)  # type: ignore[arg-type]
            .where(BigBoardConsensus.draft_year == draft_year)  # type: ignore[arg-type]
            .order_by(ConsensusSnapshot.computed_at)  # type: ignore[arg-type]
        )
    ).all()

    rank_history = [
        RankHistoryPoint(
            computed_at=row.computed_at,
            consensus_rank=row.BigBoardConsensus.consensus_rank,
            snapshot_id=row.BigBoardConsensus.snapshot_id,
        )
        for row in history_bbc_rows
    ]

    return PlayerConsensusDetail(
        player_id=player_id,
        player_name=player.display_name if player else None,
        school=player.school if player else None,
        consensus_rank=bbc.consensus_rank,
        avg_rank=bbc.avg_rank,
        median_rank=bbc.median_rank,
        high_rank=bbc.high_rank,
        low_rank=bbc.low_rank,
        std_dev=bbc.std_dev,
        num_sources=bbc.num_sources,
        prev_rank=bbc.prev_rank,
        rank_delta=bbc.rank_delta,
        source_ranks=source_ranks,
        rank_history=rank_history,
    )


async def get_source_analytics(
    db: AsyncSession,
    *,
    draft_year: int,
) -> list[SourceAnalyticsRow]:
    """Return source analytics rows for the latest snapshot of a draft year.

    Args:
        db: Async DB session.
        draft_year: Draft class to query.

    Returns:
        One row per source in the latest snapshot, ordered by
        ``contrarian_score`` desc. Empty list when no snapshot exists.
    """
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return []

    sa_rows = sorted(
        await _get_source_analytics_rows(db, sid),
        key=lambda row: row.contrarian_score,
        reverse=True,
    )

    if not sa_rows:
        return []

    source_ids = [r.news_source_id for r in sa_rows]
    source_rows = await _load_consensus_sources(db, source_ids)
    source_map = {s.id: s for s in source_rows if s.id is not None}

    out: list[SourceAnalyticsRow] = []
    for row in sa_rows:
        src = source_map.get(row.news_source_id)
        assert row.id is not None
        out.append(
            SourceAnalyticsRow(
                id=row.id,
                snapshot_id=row.snapshot_id,
                news_source_id=row.news_source_id,
                source_name=src.name if src else f"source_{row.news_source_id}",
                source_display_name=src.display_name
                if src
                else f"source_{row.news_source_id}",
                latest_board_id=row.latest_board_id,
                avg_deviation=row.avg_deviation,
                contrarian_score=row.contrarian_score,
                biggest_outlier_player_id=row.biggest_outlier_player_id,
                outlier_delta=row.outlier_delta,
                alignment=row.alignment,
            )
        )
    return out


def _alignment_score(rho: Optional[float]) -> Optional[int]:
    """Map a Spearman correlation (−1..1) to a friendly 0–100 alignment score.

    100 = ranks players exactly like consensus; 50 = no correlation; 0 = ranks
    them in the opposite order. Returns None when ``rho`` is None (the source
    ranked too few shared players for the statistic to be meaningful).
    """
    if rho is None:
        return None
    score = round((rho + 1.0) / 2.0 * 100.0)
    return max(0, min(100, score))


async def get_source_leaderboard(
    db: AsyncSession,
    *,
    draft_year: int,
    consensus_rows: Optional[list[ConsensusRow]] = None,
) -> list[dict]:
    """Return source analytics rows shaped for the /sources leaderboard page.

    Each dict includes source name, slug, contrarian score, avg deviation,
    and biggest-outlier player info. Ordered by contrarian_score desc.

    Args:
        db: Async DB session.
        draft_year: Draft class to query.
        consensus_rows: Optional rows already loaded by a composed homepage.

    Returns:
        List of dicts ready for template rendering, or empty list when no
        snapshot exists for ``draft_year``.
    """
    from app.utils.slug import generate_slug

    analytics_rows = await get_source_analytics(db, draft_year=draft_year)
    if not analytics_rows:
        return []

    outlier_player_ids = [
        r.biggest_outlier_player_id
        for r in analytics_rows
        if r.biggest_outlier_player_id is not None
    ]
    outlier_player_map: dict[int, Any] = {}
    if consensus_rows is None:
        outlier_player_map = await _player_name_map(db, outlier_player_ids)
    else:
        outlier_player_map = {row.player_id: row for row in consensus_rows}

    out: list[dict] = []
    for row in analytics_rows:
        outlier_player = (
            outlier_player_map.get(row.biggest_outlier_player_id)
            if row.biggest_outlier_player_id is not None
            else None
        )
        outlier_name = (
            getattr(outlier_player, "display_name", None)
            or getattr(outlier_player, "player_name", None)
            if outlier_player
            else None
        )
        out.append(
            {
                "news_source_id": row.news_source_id,
                "source_name": row.source_name,
                "source_display_name": row.source_display_name,
                "source_slug": generate_slug(row.source_name),
                "contrarian_score": row.contrarian_score,
                "avg_deviation": row.avg_deviation,
                "biggest_outlier_player_name": outlier_name,
                "biggest_outlier_player_slug": outlier_player.slug
                if outlier_player
                else None,
                "outlier_delta": row.outlier_delta,
                "alignment": row.alignment,
                "alignment_score": _alignment_score(row.alignment),
            }
        )
    return out


async def get_source_detail(
    db: AsyncSession,
    *,
    source_slug: str,
    draft_year: int,
) -> Optional[dict]:
    """Return detail data for a single source: its board vs consensus overlay.

    Resolves the source by slug-matching ``NewsSource.name`` (kebab-cased).
    Returns ``None`` when no source matches the slug.

    Args:
        db: Async DB session.
        source_slug: URL slug derived from ``NewsSource.name``.
        draft_year: Draft class to query.

    Returns:
        Dict with source metadata, per-player overlay rows (source rank vs
        consensus rank), and analytics summary; or ``None`` when the slug
        does not match any known source.
    """
    from app.utils.slug import generate_slug

    # --- Resolve source by slug -----------------------------------------------
    all_sources = (
        (
            await db.execute(
                select(NewsSource)  # type: ignore[call-overload]
            )
        )
        .scalars()
        .all()
    )
    matched_source: Optional[NewsSource] = None
    for src in all_sources:
        if generate_slug(src.name) == source_slug:
            matched_source = src
            break
    if matched_source is None or matched_source.id is None:
        return None

    # --- Source analytics row for this source ---------------------------------
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return None

    sa_row = (
        await db.execute(
            select(SourceAnalytics)  # type: ignore[call-overload]
            .where(SourceAnalytics.snapshot_id == sid)  # type: ignore[arg-type]
            .where(SourceAnalytics.news_source_id == matched_source.id)  # type: ignore[arg-type]
        )
    ).scalar_one_or_none()

    if sa_row is None:
        return None

    # --- Source board entries (latest approved board for this source) ----------
    source_board = (
        await db.execute(
            select(Board).where(Board.id == sa_row.latest_board_id)  # type: ignore[call-overload, arg-type]
        )
    ).scalar_one_or_none()

    source_entries: list[BoardEntry] = []
    if source_board is not None and source_board.id is not None:
        source_entries = list(
            (
                await db.execute(
                    select(BoardEntry)  # type: ignore[call-overload]
                    .where(BoardEntry.board_id == source_board.id)  # type: ignore[arg-type]
                    .order_by(BoardEntry.position)  # type: ignore[arg-type]
                )
            )
            .scalars()
            .all()
        )

    # --- Consensus board for overlay ------------------------------------------
    consensus_rows = await get_consensus_board(
        db, draft_year=draft_year, snapshot_id=sid
    )
    consensus_rank_map = {r.player_id: r for r in consensus_rows}

    # --- Build overlay rows ---------------------------------------------------
    all_player_ids = [e.player_id for e in source_entries if e.player_id is not None]
    player_map = await _player_name_map(db, all_player_ids)

    # Batch-resolve school logos (cache-backed) and player photos (from
    # PlayerImageAsset) once for the source board's player set.
    logo_map = await get_logo_urls_for_schools(
        db, [p.school for p in player_map.values()]
    )
    photo_map = await get_current_image_urls_for_players(
        db,
        player_ids=list(player_map.keys()),
        style=_CONSENSUS_PHOTO_STYLE,
    )

    # Biggest outlier player id (for highlighting)
    biggest_outlier_player_id = sa_row.biggest_outlier_player_id

    overlay_rows: list[dict] = []
    for entry in source_entries:
        pid = entry.player_id
        player = player_map.get(pid) if pid is not None else None  # type: ignore[arg-type]
        consensus_row = consensus_rank_map.get(pid) if pid is not None else None  # type: ignore[arg-type]
        delta = None
        if consensus_row is not None:
            delta = (
                entry.position - consensus_row.consensus_rank
            )  # positive = source lower
        photo_url = photo_map.get(pid) if pid is not None else None
        overlay_rows.append(
            {
                "player_id": entry.player_id,
                "player_name": player.display_name if player else None,
                "player_slug": player.slug if player else None,
                "school": player.school if player else None,
                "photo_url": photo_url,
                "school_logo_url": (
                    logo_map.get(player.school or "") if player else None
                ),
                "source_rank": entry.position,
                "consensus_rank": consensus_row.consensus_rank
                if consensus_row
                else None,
                "delta": delta,
                "is_biggest_outlier": entry.player_id == biggest_outlier_player_id,
            }
        )

    return {
        "news_source_id": matched_source.id,
        "source_name": matched_source.name,
        "source_display_name": matched_source.display_name,
        "source_slug": source_slug,
        "avg_deviation": sa_row.avg_deviation,
        "contrarian_score": sa_row.contrarian_score,
        "outlier_delta": sa_row.outlier_delta,
        "biggest_outlier_player_id": biggest_outlier_player_id,
        "alignment": sa_row.alignment,
        "alignment_score": _alignment_score(sa_row.alignment),
        "overlay_rows": overlay_rows,
        "draft_year": draft_year,
    }


async def get_source_overlays(
    db: AsyncSession,
    *,
    draft_year: int,
    consensus_rows: list[ConsensusRow],
) -> list[dict]:
    """Return every contributing source's board-vs-consensus overlay, batched.

    Produces the same per-source overlay payloads as calling
    :func:`get_source_detail` once per source, but resolves all sources,
    boards, entries, players, logos, and photos in a handful of bulk queries
    instead of re-running the whole pipeline — and rebuilding the consensus
    board — for every source. The shared ``consensus_rows`` (already built by
    the caller for the main board) supply the consensus ranks, so the board is
    not recomputed here at all.

    Args:
        db: Async DB session.
        draft_year: Draft class to query.
        consensus_rows: The current consensus board rows (caller-built);
            supplies the consensus rank each source's picks are measured
            against. Their order is irrelevant — only the rank lookup is used.

    Returns:
        One dict per source, ordered by ``contrarian_score`` desc (same order
        and shape as the per-source ``get_source_detail`` results). Empty when
        no source analytics exist for ``draft_year``.
    """
    from app.utils.slug import generate_slug

    analytics_rows = await get_source_analytics(db, draft_year=draft_year)
    if not analytics_rows:
        return []

    consensus_rank_map = {r.player_id: r.consensus_rank for r in consensus_rows}

    # Each source's latest board → bulk-fetch all their entries in one query.
    board_ids = [
        r.latest_board_id for r in analytics_rows if r.latest_board_id is not None
    ]
    entries_by_board: dict[int, list[tuple[int, int]]] = {}
    if board_ids:
        entry_rows = (
            await db.execute(
                select(  # type: ignore[call-overload]
                    BoardEntry.board_id, BoardEntry.player_id, BoardEntry.position
                )
                .where(BoardEntry.board_id.in_(board_ids))  # type: ignore[union-attr, attr-defined]
                .order_by(BoardEntry.position)  # type: ignore[arg-type]
            )
        ).all()
        for row in entry_rows:
            if row.player_id is None:
                continue
            entries_by_board.setdefault(row.board_id, []).append(
                (row.player_id, row.position)
            )

    # One batched metadata pass across every player on every source board.
    all_player_ids = list(
        {pid for entries in entries_by_board.values() for pid, _ in entries}
    )
    player_map = await _player_name_map(db, all_player_ids)
    logo_map = await get_logo_urls_for_schools(
        db, [p.school for p in player_map.values()]
    )
    photo_map = await get_current_image_urls_for_players(
        db, player_ids=all_player_ids, style=_CONSENSUS_PHOTO_STYLE
    )

    overlays: list[dict] = []
    for sa in analytics_rows:
        entries = entries_by_board.get(sa.latest_board_id or -1, [])
        overlay_rows: list[dict] = []
        for pid, source_rank in entries:
            player = player_map.get(pid)
            consensus_rank = consensus_rank_map.get(pid)
            overlay_rows.append(
                {
                    "player_id": pid,
                    "player_name": player.display_name if player else None,
                    "player_slug": player.slug if player else None,
                    "school": player.school if player else None,
                    "photo_url": photo_map.get(pid),
                    "school_logo_url": (
                        logo_map.get(player.school or "") if player else None
                    ),
                    "source_rank": source_rank,
                    "consensus_rank": consensus_rank,
                    "delta": (
                        source_rank - consensus_rank  # positive = source lower
                        if consensus_rank is not None
                        else None
                    ),
                    "is_biggest_outlier": pid == sa.biggest_outlier_player_id,
                }
            )
        overlays.append(
            {
                "news_source_id": sa.news_source_id,
                "source_name": sa.source_name,
                "source_display_name": sa.source_display_name,
                "source_slug": generate_slug(sa.source_name),
                "avg_deviation": sa.avg_deviation,
                "contrarian_score": sa.contrarian_score,
                "outlier_delta": sa.outlier_delta,
                "biggest_outlier_player_id": sa.biggest_outlier_player_id,
                "alignment": sa.alignment,
                "alignment_score": _alignment_score(sa.alignment),
                "overlay_rows": overlay_rows,
                "draft_year": draft_year,
            }
        )
    return overlays


async def get_snapshots(
    db: AsyncSession,
    *,
    draft_year: int,
    limit: int = 10,
) -> list[SnapshotSummary]:
    """Return recent snapshots for a draft year, newest first.

    Args:
        db: Async DB session.
        draft_year: Draft class to query.
        limit: Maximum number of snapshots to return.

    Returns:
        Snapshots ordered by ``computed_at`` desc, up to ``limit``.
    """
    rows = (
        (
            await db.execute(
                select(ConsensusSnapshot)  # type: ignore[call-overload]
                .where(ConsensusSnapshot.draft_year == draft_year)  # type: ignore[arg-type]
                .order_by(ConsensusSnapshot.computed_at.desc())  # type: ignore[attr-defined]
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return [
        SnapshotSummary(
            id=row.id,  # type: ignore[arg-type]
            draft_year=row.draft_year,
            computed_at=row.computed_at,
            num_boards=row.num_boards,
            trigger=row.trigger,
        )
        for row in rows
    ]


async def get_biggest_movers(
    db: AsyncSession,
    *,
    draft_year: int,
    k: int = 5,
    consensus_rows: Optional[list[ConsensusRow]] = None,
) -> dict:
    """Return the top risers and fallers between the two most recent snapshots.

    Compares ``rank_delta`` (positive = rising, negative = falling) on the
    most recent snapshot. Rows with no ``rank_delta`` (i.e. only one snapshot
    exists) are excluded, so the result may be empty.

    Args:
        db: Async DB session.
        draft_year: Draft class to query.
        k: Maximum number of risers and fallers to return (each).
        consensus_rows: Optional rows already loaded by a composed homepage.

    Returns:
        ``{"risers": [...], "fallers": [...]}`` where each item is a dict with
        ``player_id``, ``player_name``, ``slug``, ``school``, ``photo_url``,
        ``school_logo_url``, ``consensus_rank``, ``rank_delta``, and
        ``prev_rank``. Both lists are empty when no prior snapshot exists.
    """
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return {"risers": [], "fallers": []}

    if consensus_rows is not None:
        changed_rows = [row for row in consensus_rows if row.rank_delta is not None]

        def _to_consensus_mover(row: ConsensusRow) -> dict:
            return {
                "player_id": row.player_id,
                "player_name": row.player_name,
                "slug": row.slug,
                "school": row.school,
                "photo_url": row.photo_url,
                "school_logo_url": row.school_logo_url,
                "consensus_rank": row.consensus_rank,
                "rank_delta": row.rank_delta,
                "prev_rank": row.prev_rank,
            }

        cached_risers = sorted(
            [row for row in changed_rows if row.rank_delta and row.rank_delta > 0],
            key=lambda row: -(row.rank_delta or 0),
        )[:k]
        cached_fallers = sorted(
            [row for row in changed_rows if row.rank_delta and row.rank_delta < 0],
            key=lambda row: row.rank_delta or 0,
        )[:k]
        return {
            "risers": [_to_consensus_mover(row) for row in cached_risers],
            "fallers": [_to_consensus_mover(row) for row in cached_fallers],
        }

    # Fetch all rows for the current snapshot that have a non-null rank_delta.
    bbc_rows = (
        (
            await db.execute(
                select(BigBoardConsensus)  # type: ignore[call-overload]
                .where(BigBoardConsensus.snapshot_id == sid)  # type: ignore[arg-type]
                .where(BigBoardConsensus.rank_delta.is_not(None))  # type: ignore[union-attr]
            )
        )
        .scalars()
        .all()
    )

    if not bbc_rows:
        return {"risers": [], "fallers": []}

    player_ids = [r.player_id for r in bbc_rows]
    player_map = await _player_name_map(db, player_ids)
    photo_map = await get_current_image_urls_for_players(
        db, player_ids=player_ids, style=_CONSENSUS_PHOTO_STYLE
    )
    logo_map = await get_logo_urls_for_schools(
        db, [p.school for p in player_map.values()]
    )

    def _to_mover(bbc: BigBoardConsensus) -> dict:
        player = player_map.get(bbc.player_id)
        return {
            "player_id": bbc.player_id,
            "player_name": player.display_name if player else None,
            "slug": player.slug if player else None,
            "school": player.school if player else None,
            "photo_url": photo_map.get(bbc.player_id),
            "school_logo_url": (logo_map.get(player.school or "") if player else None),
            "consensus_rank": bbc.consensus_rank,
            "rank_delta": bbc.rank_delta,
            "prev_rank": bbc.prev_rank,
        }

    # rank_delta > 0 → risen (smaller rank number); sort descending by delta
    risers = sorted(
        [r for r in bbc_rows if r.rank_delta is not None and r.rank_delta > 0],
        key=lambda r: -(r.rank_delta or 0),
    )[:k]

    # rank_delta < 0 → fallen; sort ascending (most negative first)
    fallers = sorted(
        [r for r in bbc_rows if r.rank_delta is not None and r.rank_delta < 0],
        key=lambda r: r.rank_delta or 0,
    )[:k]

    return {
        "risers": [_to_mover(r) for r in risers],
        "fallers": [_to_mover(r) for r in fallers],
    }


async def get_most_controversial(
    db: AsyncSession,
    *,
    draft_year: int,
    limit: int = 5,
    min_sources: int = 2,
    consensus_rows: Optional[list[ConsensusRow]] = None,
) -> list[dict]:
    """Return the players with the widest spread of source ranks.

    Ranks players for the latest snapshot by ``std_dev`` descending — the
    larger the standard deviation of the ranks each source assigned, the more
    the boards disagree about where the player belongs. Only players ranked by
    at least ``min_sources`` boards are considered, so a single outlier ranking
    can't masquerade as "controversy".

    Args:
        db: Async DB session.
        draft_year: Draft class to query.
        limit: Maximum number of players to return.
        min_sources: Minimum number of contributing sources required for a
            player to be eligible (default 2).
        consensus_rows: Optional rows already loaded by a composed homepage.

    Returns:
        A list of dicts (most controversial first), each with ``player_id``,
        ``player_name``, ``slug``, ``consensus_rank``, ``avg_rank``,
        ``high_rank``, ``low_rank``, ``std_dev``, ``num_sources``, and
        ``source_ranks`` (the individual rank each source gave the player, so
        the UI can plot the distribution). Empty when no snapshot exists or no
        player clears ``min_sources``.
    """
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return []

    if consensus_rows is not None:
        rows = [
            row
            for row in consensus_rows
            if row.num_sources >= min_sources and (row.std_dev or 0) > 0
        ]
        rows.sort(key=lambda row: row.std_dev or 0, reverse=True)
        rows = rows[:limit]
        if not rows:
            return []
        source_ranks_map = await _source_ranks_for_players(
            db,
            snapshot_id=sid,
            player_ids=[row.player_id for row in rows],
        )
        return [
            {
                "player_id": row.player_id,
                "player_name": row.player_name,
                "slug": row.slug,
                "photo_url": row.photo_url,
                "consensus_rank": row.consensus_rank,
                "avg_rank": row.avg_rank,
                "high_rank": row.high_rank,
                "low_rank": row.low_rank,
                "std_dev": row.std_dev,
                "num_sources": row.num_sources,
                "source_ranks": source_ranks_map.get(row.player_id, []),
            }
            for row in rows
        ]

    bbc_rows = (
        (
            await db.execute(
                select(BigBoardConsensus)  # type: ignore[call-overload]
                .where(BigBoardConsensus.snapshot_id == sid)  # type: ignore[arg-type]
                .where(BigBoardConsensus.num_sources >= min_sources)  # type: ignore[arg-type]
                .where(BigBoardConsensus.std_dev > 0)  # type: ignore[arg-type]
                .order_by(BigBoardConsensus.std_dev.desc())  # type: ignore[attr-defined]
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    if not bbc_rows:
        return []

    player_ids = [r.player_id for r in bbc_rows]
    player_map = await _player_name_map(db, player_ids)
    photo_map = await get_current_image_urls_for_players(
        db, player_ids=player_ids, style=_CONSENSUS_PHOTO_STYLE
    )
    source_ranks_map = await _source_ranks_for_players(
        db, snapshot_id=sid, player_ids=player_ids
    )

    result: list[dict] = []
    for bbc in bbc_rows:
        player = player_map.get(bbc.player_id)
        result.append(
            {
                "player_id": bbc.player_id,
                "player_name": player.display_name if player else None,
                "slug": player.slug if player else None,
                "photo_url": photo_map.get(bbc.player_id),
                "consensus_rank": bbc.consensus_rank,
                "avg_rank": bbc.avg_rank,
                "high_rank": bbc.high_rank,
                "low_rank": bbc.low_rank,
                "std_dev": bbc.std_dev,
                "num_sources": bbc.num_sources,
                "source_ranks": source_ranks_map.get(bbc.player_id, []),
            }
        )
    return result


async def _source_ranks_for_players(
    db: AsyncSession,
    *,
    snapshot_id: int,
    player_ids: list[int],
) -> dict[int, list[int]]:
    """Return ``player_id -> [source ranks]`` for one snapshot.

    Gathers the individual rank each contributing board assigned to each
    player (across the boards that fed the snapshot), so callers can plot the
    spread of opinion rather than just its min/max.
    """
    if not player_ids:
        return {}

    snapshot = await _load_consensus_snapshot(db, snapshot_id)
    if snapshot is None or not snapshot.board_ids:
        return {}

    entry_cache = db.info.get(_CONSENSUS_ENTRY_CACHE_KEY)
    if isinstance(entry_cache, dict):
        # The composed homepage warms this cache with full board entries for
        # the source spotlight panel, so reuse that read when it is available.
        entry_rows = await _load_consensus_entries(db, snapshot.board_ids)
    else:
        # Standalone consensus calls only need ranks for the requested players;
        # avoid loading every entry in every contributing board.
        entry_rows = list(
            (
                await db.execute(
                    select(BoardEntry)
                    .where(  # type: ignore[call-overload]
                        cast(Any, BoardEntry.board_id).in_(snapshot.board_ids)
                    )
                    .where(cast(Any, BoardEntry.player_id).in_(player_ids))
                )
            )
            .scalars()
            .all()
        )

    ranks: dict[int, list[int]] = {pid: [] for pid in player_ids}
    for row in entry_rows:
        if row.player_id in ranks:
            ranks[row.player_id].append(row.position)
    for pid in ranks:
        ranks[pid].sort()
    return ranks


# Per-award accent colors (CSS custom-property references resolved in the
# template via ``--slot-accent``). One per axis so paired slots read distinct.
_AWARD_ACCENTS = {
    "boldest": "var(--color-accent-rose)",
    "ahead": "var(--color-accent-amber)",
    "deepest": "var(--color-accent-cyan)",
    "freshest": "var(--color-accent-emerald)",
}


def _standout(value: float, values: list[float]) -> float:
    """Return how far ``value`` stands out from ``values`` (a z-score).

    Used as a cross-award "newsworthiness" score: the more lopsided a winner
    is versus the field, the higher it ranks for the spotlight. Returns 0 when
    there's nothing to compare against or the field is uniform (in which case
    that award isn't differentiating and shouldn't lead).
    """
    if len(values) < 2:
        return 0.0
    sd = statistics.pstdev(values)
    if sd == 0:
        return 0.0
    return (value - statistics.fmean(values)) / sd


async def _build_source_profiles(
    db: AsyncSession,
    sa_rows: list[SourceAnalytics],
    consensus_by_player: dict[int, ConsensusRow],
) -> dict[int, dict]:
    """Build a per-source profile for every contributing source.

    Each profile carries the metrics the award engine needs: the source's
    board overlay vs consensus (reaches/fades), board size, freshness, link to
    their published board, and the candidate "highlight" picks (biggest
    outlier, deepest pick, best validated reach).
    """
    from app.utils.slug import generate_slug

    board_ids = [sa.latest_board_id for sa in sa_rows if sa.latest_board_id is not None]

    boards = await _load_consensus_boards(db, board_ids)
    board_by_id = {b.id: b for b in boards if b.id is not None}

    # All entries for those boards (not just consensus players) so board size
    # reflects the full depth the source actually ranked.
    entry_rows = await _load_consensus_entries(db, board_ids)
    entries_by_board: dict[int, list[tuple[int, int]]] = {}
    for row in entry_rows:
        if row.board_id is not None and row.player_id is not None:
            entries_by_board.setdefault(row.board_id, []).append(
                (row.player_id, row.position)
            )

    sources = await _load_consensus_sources(db, [sa.news_source_id for sa in sa_rows])
    source_by_id = {s.id: s for s in sources if s.id is not None}

    article_ids = [b.news_item_id for b in boards if b.news_item_id is not None]
    article_by_id: dict[int, NewsItem] = {}
    if article_ids:
        article_by_id = {
            a.id: a
            for a in (
                await db.execute(
                    select(NewsItem).where(  # type: ignore[call-overload]
                        NewsItem.id.in_(article_ids)  # type: ignore[union-attr]
                    )
                )
            )
            .scalars()
            .all()
            if a.id is not None
        }

    profiles: dict[int, dict] = {}
    for sa in sa_rows:
        src = source_by_id.get(sa.news_source_id)
        source_name = src.name if src else f"source_{sa.news_source_id}"
        board = board_by_id.get(sa.latest_board_id) if sa.latest_board_id else None
        entries = entries_by_board.get(sa.latest_board_id or -1, [])

        # Link out to the producer's own published board when available.
        work_url: Optional[str] = None
        work_title: Optional[str] = None
        if board is not None and board.news_item_id is not None:
            article = article_by_id.get(board.news_item_id)
            if article is not None:
                work_url, work_title = article.url, article.title

        # Overlay vs consensus → reaches/fades + candidate picks.
        reaches = fades = 0
        deepest: Optional[dict] = None
        ahead: Optional[dict] = None
        for pid, source_rank in entries:
            crow = consensus_by_player.get(pid)
            if crow is None:
                continue
            delta = crow.consensus_rank - source_rank  # + = source ranks higher
            if delta > 0:
                reaches += 1
            elif delta < 0:
                fades += 1
            if deepest is None or source_rank > deepest["source_rank"]:
                deepest = {"player_id": pid, "source_rank": source_rank}
            # Validated reach: source is high on a player consensus is rising
            # toward (positive rank_delta = moved up since the prior snapshot).
            if delta > 0 and crow.rank_delta is not None and crow.rank_delta > 0:
                if ahead is None or crow.rank_delta > ahead["value"]:
                    ahead = {
                        "player_id": pid,
                        "source_rank": source_rank,
                        "consensus_rank": crow.consensus_rank,
                        "value": crow.rank_delta,
                    }

        # Biggest outlier (precomputed on the analytics row).
        outlier: Optional[dict] = None
        out_pid = sa.biggest_outlier_player_id
        if out_pid is not None and out_pid in consensus_by_player:
            crank = consensus_by_player[out_pid].consensus_rank
            outlier = {
                "player_id": out_pid,
                "source_rank": crank - sa.outlier_delta,
                "consensus_rank": crank,
            }

        profiles[sa.news_source_id] = {
            "source_id": sa.news_source_id,
            "source_display_name": src.display_name if src else source_name,
            "source_slug": generate_slug(source_name),
            "work_url": work_url,
            "work_title": work_title,
            "avg_deviation": sa.avg_deviation,
            "board_size": board.size if board is not None else len(entries),
            "published_at": board.published_at if board is not None else None,
            "reaches": reaches,
            "fades": fades,
            "outlier": outlier,
            "deepest": deepest,
            "ahead": ahead,
        }
    return profiles


def _highlight(
    consensus_by_player: dict[int, ConsensusRow],
    player_id: int,
    *,
    label: str,
    detail: str,
) -> Optional[dict]:
    """Build a player-highlight payload from a consensus row (photo + name)."""
    crow = consensus_by_player.get(player_id)
    if crow is None:
        return None
    return {
        "label": label,
        "player_name": crow.player_name,
        "slug": crow.slug,
        "photo_url": crow.photo_url,
        "detail": detail,
    }


async def get_source_spotlight(
    db: AsyncSession,
    *,
    draft_year: int,
    consensus_rows: Optional[list[ConsensusRow]] = None,
) -> Optional[dict]:
    """Return two spotlight-worthy contributors, each with a distinct award.

    Computes a pool of awards on different axes — Boldest Board (divergence),
    Deepest Board (depth), Ahead of the Curve (foresight, proxy), Freshest Take
    (recency) — scores each winner by how far it stands out from the field, and
    picks the two most newsworthy awards that go to *different* contributors.
    This avoids crowning the same source twice and keeps the spotlight rotating.

    Args:
        db: Async DB session.
        draft_year: Draft class to query.
        consensus_rows: The current consensus board rows, when the caller has
            already built them (e.g. the consensus page). Supplied to avoid
            rebuilding the board here; ``None`` lets this helper fetch the
            latest board itself.

    Returns:
        ``{"slots": [slot, ...]}`` with one or two slots, or ``None`` when no
        source analytics exist. Each slot is a render-ready dict.
    """
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return None

    sa_rows = await _get_source_analytics_rows(db, sid)
    if not sa_rows:
        return None

    if consensus_rows is None:
        consensus_rows = await get_consensus_board(
            db, draft_year=draft_year, snapshot_id=sid
        )
    consensus_by_player = {r.player_id: r for r in consensus_rows}

    profiles = await _build_source_profiles(db, sa_rows, consensus_by_player)
    if not profiles:
        return None
    plist = list(profiles.values())

    def _slot(p: dict, *, key: str, label: str, stat_text: str, highlight) -> dict:
        return {
            "award_key": key,
            "award_label": label,
            "accent_css": _AWARD_ACCENTS[key],
            "source_display_name": p["source_display_name"],
            "source_slug": p["source_slug"],
            "work_url": p["work_url"],
            "work_title": p["work_title"],
            "stat_text": stat_text,
            "reaches": p["reaches"],
            "fades": p["fades"],
            "highlight": highlight,
        }

    awards: list[dict] = []

    # Boldest Board — widest average divergence from consensus.
    boldest = max(plist, key=lambda p: p["avg_deviation"])
    awards.append(
        {
            "winner_id": boldest["source_id"],
            "newsworthiness": _standout(
                boldest["avg_deviation"], [p["avg_deviation"] for p in plist]
            ),
            "slot": _slot(
                boldest,
                key="boldest",
                label="Boldest Board",
                stat_text=f"Avg deviation {boldest['avg_deviation']:.1f} spots",
                highlight=(
                    _highlight(
                        consensus_by_player,
                        boldest["outlier"]["player_id"],
                        label="Boldest call",
                        detail=(
                            f"#{boldest['outlier']['source_rank']} "
                            f"· consensus #{boldest['outlier']['consensus_rank']}"
                        ),
                    )
                    if boldest["outlier"]
                    else None
                ),
            ),
        }
    )

    # Deepest Board — most prospects ranked.
    deepest = max(plist, key=lambda p: p["board_size"])
    awards.append(
        {
            "winner_id": deepest["source_id"],
            "newsworthiness": _standout(
                deepest["board_size"], [p["board_size"] for p in plist]
            ),
            "slot": _slot(
                deepest,
                key="deepest",
                label="Deepest Board",
                stat_text=f"Ranks {deepest['board_size']} prospects",
                highlight=(
                    _highlight(
                        consensus_by_player,
                        deepest["deepest"]["player_id"],
                        label="Deepest pick",
                        detail=f"#{deepest['deepest']['source_rank']}",
                    )
                    if deepest["deepest"]
                    else None
                ),
            ),
        }
    )

    # Freshest Take — most recently published board.
    dated = [p for p in plist if p["published_at"] is not None]
    if dated:
        freshest = max(dated, key=lambda p: p["published_at"])
        awards.append(
            {
                "winner_id": freshest["source_id"],
                "newsworthiness": _standout(
                    freshest["published_at"].timestamp(),
                    [p["published_at"].timestamp() for p in dated],
                ),
                "slot": _slot(
                    freshest,
                    key="freshest",
                    label="Freshest Take",
                    stat_text=f"Updated {freshest['published_at'].strftime('%b %-d')}",
                    highlight=None,
                ),
            }
        )

    # Ahead of the Curve (proxy) — boldest reach the field is moving toward.
    ahead_cands = [p for p in plist if p["ahead"] is not None]
    if ahead_cands:
        ahead_winner = max(ahead_cands, key=lambda p: p["ahead"]["value"])
        awards.append(
            {
                "winner_id": ahead_winner["source_id"],
                "newsworthiness": _standout(
                    ahead_winner["ahead"]["value"],
                    [(p["ahead"]["value"] if p["ahead"] else 0) for p in plist],
                ),
                "slot": _slot(
                    ahead_winner,
                    key="ahead",
                    label="Ahead of the Curve",
                    stat_text="The field is catching up",
                    highlight=_highlight(
                        consensus_by_player,
                        ahead_winner["ahead"]["player_id"],
                        label="Called early",
                        detail=(
                            f"had #{ahead_winner['ahead']['source_rank']} "
                            f"· consensus #{ahead_winner['ahead']['consensus_rank']} ↑"
                        ),
                    ),
                ),
            }
        )

    # Pick the two most newsworthy awards that go to different contributors.
    awards.sort(key=lambda a: a["newsworthiness"], reverse=True)
    slots = [awards[0]["slot"]]
    second = next(
        (a for a in awards[1:] if a["winner_id"] != awards[0]["winner_id"]), None
    )
    if second is not None:
        slots.append(second["slot"])

    return {"slots": slots}


async def get_live_board_ids(
    db: AsyncSession,
    *,
    draft_years: Iterable[int],
) -> set[int]:
    """Return the board IDs feeding the latest snapshot for each draft year.

    A board is "live" in the consensus when its id appears in the
    ``board_ids`` of the most recent snapshot for its draft year. Approved
    boards superseded by a newer board from the same source are absent from
    the set, as are pending/rejected boards.

    Args:
        db: Async DB session.
        draft_years: Draft years to resolve snapshots for; duplicates are
            collapsed.

    Returns:
        The union of ``board_ids`` across the latest snapshot of each
        requested draft year. Empty when no snapshots exist.
    """
    live: set[int] = set()
    for year in set(draft_years):
        sid = await _resolve_snapshot_id(db, draft_year=year, snapshot_id=None)
        if sid is None:
            continue
        board_ids = await db.scalar(
            select(ConsensusSnapshot.board_ids).where(  # type: ignore[call-overload]
                ConsensusSnapshot.id == sid  # type: ignore[arg-type]
            )
        )
        if board_ids:
            live.update(board_ids)
    return live


async def get_board_freshness(
    db: AsyncSession,
    *,
    draft_year: int,
) -> Optional[dict]:
    """Return freshness metadata for the latest snapshot.

    Derives board count, unique source count, and the latest ``published_at``
    date from the APPROVED boards that fed the current snapshot.

    Args:
        db: Async DB session.
        draft_year: Draft class to query.

    Returns:
        Dict with ``num_boards``, ``num_sources``, and ``last_updated``
        (a ``datetime``), or ``None`` when no snapshot exists.
    """
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return None

    snapshot = (
        await db.execute(
            select(ConsensusSnapshot).where(  # type: ignore[call-overload]
                ConsensusSnapshot.id == sid  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()

    if snapshot is None or not snapshot.board_ids:
        return None

    board_rows = [
        board
        for board in await _load_consensus_boards(db, snapshot.board_ids)
        if board.status == BoardStatus.APPROVED
    ]

    if not board_rows:
        return None

    unique_sources = len({b.news_source_id for b in board_rows})
    last_updated = max(
        (b.approved_at for b in board_rows if b.approved_at is not None),
        default=snapshot.computed_at,
    )

    return {
        "num_boards": len(board_rows),
        "num_sources": unique_sources,
        "last_updated": last_updated,
    }


# ---------------------------------------------------------------------------
# Consensus-page helpers (ticket #270)
# ---------------------------------------------------------------------------

# Outlier threshold: a source's rank for a player is flagged when it deviates
# from that player's consensus_rank by more than this many positions.
# A threshold of 5 keeps noise-free while catching meaningful divergences (e.g.
# a source has a player at #2 while consensus has them at #9).  Callers that
# want a different sensitivity can pass their own value via a future parameter
# — keeping it a named constant here makes it visible and easy to adjust.
_OUTLIER_THRESHOLD = 5


async def get_source_breakdown_matrix(
    db: AsyncSession,
    *,
    draft_year: int,
    top_n: int = 10,
) -> dict:
    """Return the top-N players × contributing sources rank matrix with outlier flags.

    Each cell records the rank a source assigned to a player and whether that
    rank diverges from consensus beyond ``_OUTLIER_THRESHOLD`` positions.

    Outlier flag definition:
        ``delta = source_rank − consensus_rank``
        - ``"high"``  → source ranks the player *better* than consensus
          (delta < −_OUTLIER_THRESHOLD, i.e. source is high on the player)
        - ``"low"``   → source ranks the player *worse* than consensus
          (delta > +_OUTLIER_THRESHOLD)
        - ``None``    → within the threshold or player absent from that source

    Args:
        db: Async DB session.
        draft_year: Draft class to query.
        top_n: Number of top consensus players to include as matrix rows.

    Returns:
        A dict with three keys::

            {
                "players": [
                    {"player_id": int, "player_name": str|None,
                     "slug": str|None, "consensus_rank": int},
                    ...
                ],
                "sources": [
                    {"source_id": int, "name": str, "slug": str},
                    ...
                ],
                "cells": {
                    (player_id, source_id): {
                        "rank": int,
                        "outlier": "high" | "low" | None,
                    },
                    ...
                },
            }

        Returns ``{"players": [], "sources": [], "cells": {}}`` when no
        snapshot exists for ``draft_year`` or the board is empty.
    """
    from app.utils.slug import generate_slug

    _empty: dict = {"players": [], "sources": [], "cells": {}}

    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return _empty

    # --- Top-N consensus rows (ordered by consensus_rank) ---------------------
    bbc_rows = (
        (
            await db.execute(
                select(BigBoardConsensus)  # type: ignore[call-overload]
                .where(BigBoardConsensus.snapshot_id == sid)  # type: ignore[arg-type]
                .order_by(BigBoardConsensus.consensus_rank)  # type: ignore[arg-type]
                .limit(top_n)
            )
        )
        .scalars()
        .all()
    )
    if not bbc_rows:
        return _empty

    consensus_rank_map: dict[int, int] = {
        r.player_id: r.consensus_rank for r in bbc_rows
    }
    top_player_ids = [r.player_id for r in bbc_rows]

    # --- Snapshot board_ids → per-source latest boards ------------------------
    snapshot = (
        await db.execute(
            select(ConsensusSnapshot).where(  # type: ignore[call-overload]
                ConsensusSnapshot.id == sid  # type: ignore[arg-type]
            )
        )
    ).scalar_one_or_none()
    if snapshot is None or not snapshot.board_ids:
        return _empty

    board_rows = (
        (
            await db.execute(
                select(Board).where(  # type: ignore[call-overload]
                    Board.id.in_(snapshot.board_ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    if not board_rows:
        return _empty

    # One board per source (last one wins when a source has multiple boards in
    # the snapshot — unlikely in practice but safe to handle).
    board_by_source: dict[int, Board] = {}
    for board in board_rows:
        if board.news_source_id is not None and board.id is not None:
            board_by_source[board.news_source_id] = board

    source_ids = list(board_by_source.keys())
    source_rows = (
        (
            await db.execute(
                select(NewsSource).where(  # type: ignore[call-overload]
                    NewsSource.id.in_(source_ids)  # type: ignore[union-attr]
                )
            )
        )
        .scalars()
        .all()
    )
    source_map: dict[int, NewsSource] = {
        s.id: s for s in source_rows if s.id is not None
    }

    # --- Per-source article URL (links each cell back to the original mock) ---
    # Resolve Board.news_item_id → NewsItem.url so every rank in the matrix can
    # link out to the board it came from. Legacy boards with no news_item carry
    # no URL and stay unlinked.
    article_news_item_ids = [
        b.news_item_id for b in board_by_source.values() if b.news_item_id is not None
    ]
    matrix_article_map: dict[int, NewsItem] = {}
    if article_news_item_ids:
        matrix_article_rows = (
            (
                await db.execute(
                    select(NewsItem).where(  # type: ignore[call-overload]
                        NewsItem.id.in_(article_news_item_ids)  # type: ignore[union-attr]
                    )
                )
            )
            .scalars()
            .all()
        )
        matrix_article_map = {a.id: a for a in matrix_article_rows if a.id is not None}
    source_work_url: dict[int, Optional[str]] = {}
    for sid_key, board in board_by_source.items():
        article = (
            matrix_article_map.get(board.news_item_id)
            if board.news_item_id is not None
            else None
        )
        source_work_url[sid_key] = article.url if article else None

    # --- Board entries for the top-N players, across all snapshot boards ------
    entry_rows = (
        await db.execute(
            select(  # type: ignore[call-overload]
                BoardEntry.board_id,
                BoardEntry.player_id,
                BoardEntry.position,
            )
            .where(BoardEntry.board_id.in_(snapshot.board_ids))  # type: ignore[union-attr, attr-defined]
            .where(BoardEntry.player_id.in_(top_player_ids))  # type: ignore[union-attr, attr-defined]
        )
    ).all()

    # board_id → source_id lookup (reverse of board_by_source)
    board_to_source: dict[int, int] = {
        b.id: sid_  # type: ignore[misc]
        for sid_, b in board_by_source.items()
        if b.id is not None
    }

    # Build cells: (player_id, source_id) → rank
    raw_cells: dict[tuple[int, int], int] = {}
    for row in entry_rows:
        src_id = board_to_source.get(row.board_id)
        if src_id is None or row.player_id is None:
            continue
        raw_cells[(row.player_id, src_id)] = row.position

    # --- Player metadata ------------------------------------------------------
    player_map = await _player_name_map(db, top_player_ids)

    # --- Assemble output ------------------------------------------------------
    players_out = [
        {
            "player_id": r.player_id,
            "player_name": player_map[r.player_id].display_name
            if r.player_id in player_map
            else None,
            "slug": player_map[r.player_id].slug if r.player_id in player_map else None,
            "consensus_rank": r.consensus_rank,
        }
        for r in bbc_rows
    ]

    sources_out = [
        {
            "source_id": src_id,
            "name": source_map[src_id].name
            if src_id in source_map
            else f"source_{src_id}",
            "slug": generate_slug(
                source_map[src_id].name if src_id in source_map else f"source_{src_id}"
            ),
            # External board URL when the source's board links to an article;
            # None for legacy boards with no news_item.
            "work_url": source_work_url.get(src_id),
        }
        for src_id in source_ids
    ]

    cells_out: dict[tuple[int, int], dict] = {}
    for (pid, src_id), source_rank in raw_cells.items():
        consensus_rank = consensus_rank_map.get(pid)
        outlier: Optional[str] = None
        delta: Optional[int] = None
        if consensus_rank is not None:
            # delta = source_rank − consensus_rank.
            #   negative → source ranks the player higher (better) than consensus
            #   positive → source ranks the player lower (worse) than consensus
            delta = source_rank - consensus_rank
            if delta < -_OUTLIER_THRESHOLD:
                outlier = "high"  # source likes the player more than consensus
            elif delta > _OUTLIER_THRESHOLD:
                outlier = "low"  # source is cooler on the player than consensus
        cells_out[(pid, src_id)] = {
            "rank": source_rank,
            "outlier": outlier,
            "delta": delta,
        }

    return {"players": players_out, "sources": sources_out, "cells": cells_out}


async def get_rank_trajectories(
    db: AsyncSession,
    *,
    draft_year: int,
    top_n: int = 10,
) -> list[dict]:
    """Return each top-N player's consensus-rank-over-time series, batched.

    Mirrors ``_recent_ranks_map`` but returns the full time series (not just
    a sparkline window) and includes the ``computed_at`` timestamp per point so
    the UI can plot meaningful x-axis labels.

    The series for each player is ordered oldest-first.  A player with only one
    snapshot yields a single-element ``series`` list (flat trajectory).

    Args:
        db: Async DB session.
        draft_year: Draft class to query.
        top_n: Number of top consensus players (by latest snapshot) to include.

    Returns:
        A list of dicts, one per player, ordered by current consensus rank::

            [
                {
                    "player_id": int,
                    "player_name": str | None,
                    "slug": str | None,
                    "series": [
                        {"computed_at": datetime, "consensus_rank": int},
                        ...  # oldest → newest
                    ],
                },
                ...
            ]

        Returns ``[]`` when no snapshot exists for ``draft_year`` or the board
        is empty.
    """
    sid = await _resolve_snapshot_id(db, draft_year=draft_year, snapshot_id=None)
    if sid is None:
        return []

    # Top-N from the latest snapshot (defines which players appear).
    bbc_latest = (
        (
            await db.execute(
                select(BigBoardConsensus)  # type: ignore[call-overload]
                .where(BigBoardConsensus.snapshot_id == sid)  # type: ignore[arg-type]
                .order_by(BigBoardConsensus.consensus_rank)  # type: ignore[arg-type]
                .limit(top_n)
            )
        )
        .scalars()
        .all()
    )
    if not bbc_latest:
        return []

    top_player_ids = [r.player_id for r in bbc_latest]

    # One batched query for all snapshots for these players, oldest-first.
    stmt = (
        select(  # type: ignore[call-overload]
            BigBoardConsensus.player_id,
            BigBoardConsensus.consensus_rank,
            ConsensusSnapshot.computed_at,
        )
        .join(
            ConsensusSnapshot,
            ConsensusSnapshot.id == BigBoardConsensus.snapshot_id,  # type: ignore[arg-type]
        )
        .where(BigBoardConsensus.draft_year == draft_year)  # type: ignore[arg-type]
        .where(
            BigBoardConsensus.player_id.in_(top_player_ids)  # type: ignore[attr-defined]
        )
        .order_by(
            BigBoardConsensus.player_id,  # type: ignore[arg-type]
            ConsensusSnapshot.computed_at,  # type: ignore[arg-type]
        )
    )
    history_rows = (await db.execute(stmt)).all()

    # Group into per-player series (already ordered oldest-first within each
    # player group because we sort by player_id, computed_at).
    series_map: dict[int, list[dict]] = {pid: [] for pid in top_player_ids}
    for pid, consensus_rank, computed_at in history_rows:
        if pid in series_map:
            series_map[pid].append(
                {"computed_at": computed_at, "consensus_rank": consensus_rank}
            )

    # Player metadata
    player_map = await _player_name_map(db, top_player_ids)

    # Return in the same order as the latest snapshot (consensus_rank asc).
    return [
        {
            "player_id": r.player_id,
            "player_name": player_map[r.player_id].display_name
            if r.player_id in player_map
            else None,
            "slug": player_map[r.player_id].slug if r.player_id in player_map else None,
            "series": series_map.get(r.player_id, []),
        }
        for r in bbc_latest
    ]
