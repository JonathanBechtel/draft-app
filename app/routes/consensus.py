"""Public read-only API for consensus mock draft and big board data.

Endpoints:
    GET /api/consensus                         Board-level ranked list.
    GET /api/consensus/player/{player_id}      Per-player detail + history.
    GET /api/consensus/sources                 Per-source analytics.
    GET /api/consensus/snapshots               Recent snapshot summaries.

All endpoints return 200 with an empty list/body rather than 404 when
data simply does not exist yet for a given year or kind (e.g. mock_draft
data will be empty until that extraction ticket ships). A missing player
in the player detail endpoint raises 404.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consensus import (
    ConsensusRow,
    PlayerConsensusDetail,
    SnapshotSummary,
    SourceAnalyticsRow,
)
from app.schemas.boards import BoardKind
from app.services import consensus_read_service as svc
from app.utils.db_async import get_session

router = APIRouter(prefix="/api/consensus", tags=["consensus"])


@router.get("", response_model=list[ConsensusRow], status_code=200)
async def get_consensus(
    draft_year: int = Query(..., description="Draft class year (e.g. 2026)"),
    kind: BoardKind = Query(..., description="Board kind: BIG_BOARD or MOCK_DRAFT"),
    snapshot_id: Optional[int] = Query(
        default=None, description="Specific snapshot id; defaults to the latest"
    ),
    db: AsyncSession = Depends(get_session),
) -> list[ConsensusRow]:
    """Return the latest (or specified) consensus board for a draft year.

    Rows are ordered by ``consensus_rank`` asc. Returns an empty list
    when no snapshot exists for the requested year/kind (e.g. MOCK_DRAFT
    data has not been computed yet).
    """
    if kind == BoardKind.MOCK_DRAFT:
        # Mock-draft consensus computation is a separate ticket; no data yet.
        return []
    return await svc.get_consensus_board(
        db,
        draft_year=draft_year,
        snapshot_id=snapshot_id,
    )


@router.get(
    "/player/{player_id}",
    response_model=PlayerConsensusDetail,
    status_code=200,
)
async def get_player_consensus(
    player_id: int,
    draft_year: int = Query(..., description="Draft class year (e.g. 2026)"),
    kind: BoardKind = Query(..., description="Board kind: BIG_BOARD or MOCK_DRAFT"),
    db: AsyncSession = Depends(get_session),
) -> PlayerConsensusDetail:
    """Return full consensus detail for a single player.

    Includes the current consensus row, per-source rank breakdown, and
    rank history (trajectory data). Raises 404 when the player has no
    current consensus row for the requested year/kind.
    """
    if kind == BoardKind.MOCK_DRAFT:
        raise HTTPException(
            status_code=404,
            detail="No mock-draft consensus data available yet for this player.",
        )
    detail = await svc.get_player_consensus_detail(
        db,
        player_id=player_id,
        draft_year=draft_year,
    )
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"No consensus data found for player_id={player_id} in {draft_year}.",
        )
    return detail


@router.get(
    "/sources",
    response_model=list[SourceAnalyticsRow],
    status_code=200,
)
async def get_source_analytics(
    draft_year: int = Query(..., description="Draft class year (e.g. 2026)"),
    kind: BoardKind = Query(..., description="Board kind: BIG_BOARD or MOCK_DRAFT"),
    db: AsyncSession = Depends(get_session),
) -> list[SourceAnalyticsRow]:
    """Return per-source deviation analytics for the latest snapshot.

    One row per source in the most recent snapshot. Returns an empty list
    when no snapshot exists for the requested year/kind.
    """
    if kind == BoardKind.MOCK_DRAFT:
        return []
    return await svc.get_source_analytics(db, draft_year=draft_year)


@router.get(
    "/snapshots",
    response_model=list[SnapshotSummary],
    status_code=200,
)
async def get_snapshots(
    draft_year: int = Query(..., description="Draft class year (e.g. 2026)"),
    kind: BoardKind = Query(..., description="Board kind: BIG_BOARD or MOCK_DRAFT"),
    limit: int = Query(
        default=10, ge=1, le=100, description="Max number of snapshots to return"
    ),
    db: AsyncSession = Depends(get_session),
) -> list[SnapshotSummary]:
    """Return recent snapshot summaries for a draft year, newest first.

    Returns an empty list when no snapshots exist for the requested
    year/kind. ``limit`` caps the response size (max 100).
    """
    if kind == BoardKind.MOCK_DRAFT:
        return []
    return await svc.get_snapshots(db, draft_year=draft_year, limit=limit)
