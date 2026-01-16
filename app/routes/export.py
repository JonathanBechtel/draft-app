"""Image export API endpoints for share card generation."""

from typing import Literal, Optional
from urllib.parse import urljoin

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.share_cards import ImageExportService
from app.utils.db_async import get_session

router = APIRouter(prefix="/api/export", tags=["export"])


class ExportContext(BaseModel):
    """Context options for image export."""

    comparison_group: Literal[
        "current_draft", "current_nba", "all_time_draft", "all_time_nba"
    ] = "current_draft"
    same_position: bool = False
    metric_group: Literal["anthropometrics", "combine", "shooting", "advanced"] = (
        "anthropometrics"
    )


class ExportRequest(BaseModel):
    """Request body for image export."""

    component: Literal["vs_arena", "performance", "h2h", "comps"]
    player_ids: list[int] = Field(..., min_length=1, max_length=2)
    context: ExportContext = Field(default_factory=ExportContext)
    redirect_path: Optional[str] = Field(
        default=None,
        max_length=2048,
        description="Optional same-origin path to redirect share links back to (e.g., /players/cooper-flagg).",
    )


class ExportResponse(BaseModel):
    """Response body for image export."""

    export_id: str
    url: str
    title: str
    filename: str
    cached: bool
    share_url: str
    debug_svg_url: Optional[str] = None  # Dev only


@router.post("/image", response_model=ExportResponse, status_code=200)
async def export_image(
    request: ExportRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_session),
) -> ExportResponse:
    """Generate a shareable PNG image for the specified component.

    Args:
        request: Export request with component, player_ids, and context
        http_request: Incoming HTTP request (used to construct absolute share URLs)
        db: Database session

    Returns:
        Export response with URL, title, filename, and cache status

    Raises:
        HTTPException: 400 for invalid request, 404 for player not found
    """
    # Validate player count based on component
    if request.component in ("vs_arena", "h2h"):
        if len(request.player_ids) != 2:
            raise HTTPException(
                status_code=400,
                detail="vs_arena and h2h require exactly 2 player_ids",
            )
    else:
        if len(request.player_ids) != 1:
            raise HTTPException(
                status_code=400,
                detail="performance and comps require exactly 1 player_id",
            )

    service = ImageExportService(db)
    try:
        redirect_path: Optional[str] = None
        if request.redirect_path:
            candidate = request.redirect_path.strip()
            if (
                candidate.startswith("/")
                and not candidate.startswith("//")
                and "://" not in candidate
                and "\n" not in candidate
                and "\r" not in candidate
                and "\t" not in candidate
            ):
                redirect_path = candidate

        result = await service.export(
            component=request.component,
            player_ids=request.player_ids,
            context=request.context.model_dump(),
            redirect_path=redirect_path,
        )
        export_id = result.get("export_id")
        if not isinstance(export_id, str) or not export_id:
            raise HTTPException(status_code=500, detail="Export missing export_id")

        share_path = f"/share/{request.component}/{export_id}"
        share_url = urljoin(str(http_request.base_url), share_path.lstrip("/"))

        return ExportResponse(
            **result,
            share_url=share_url,
        )
    except ValueError as e:
        error_msg = str(e)
        if "player_not_found" in error_msg:
            raise HTTPException(status_code=404, detail="Player not found")
        raise HTTPException(status_code=400, detail=error_msg)
