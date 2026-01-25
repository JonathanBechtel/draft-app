"""Admin NewsItem CRUD routes.

Provides read, update, and delete for news items. No create route since items
are ingested from RSS feeds.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import base_context, require_admin
from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import NewsSource
from app.schemas.players_master import PlayerMaster
from app.utils.db_async import get_session

router = APIRouter(prefix="/news-items", tags=["admin-news-items"])

# Default pagination values
DEFAULT_LIMIT = 25
MAX_LIMIT = 100


@router.get("", response_class=HTMLResponse)
async def list_news_items(
    request: Request,
    success: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    source_id: int | None = Query(default=None),
    tag: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List all news items with pagination and filters (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    # Build base query
    query = select(NewsItem).order_by(NewsItem.published_at.desc())  # type: ignore[attr-defined]
    count_query = select(func.count(NewsItem.id))  # type: ignore[arg-type]

    # Apply filters
    if source_id is not None:
        query = query.where(NewsItem.source_id == source_id)  # type: ignore[arg-type]
        count_query = count_query.where(NewsItem.source_id == source_id)  # type: ignore[arg-type]

    if tag:
        try:
            tag_enum = NewsItemTag(tag)
            query = query.where(NewsItem.tag == tag_enum)  # type: ignore[arg-type]
            count_query = count_query.where(NewsItem.tag == tag_enum)  # type: ignore[arg-type]
        except ValueError:
            pass  # Invalid tag, ignore filter

    if date_from:
        try:
            from_dt = datetime.fromisoformat(date_from)
            query = query.where(NewsItem.published_at >= from_dt)  # type: ignore[arg-type]
            count_query = count_query.where(NewsItem.published_at >= from_dt)  # type: ignore[arg-type]
        except ValueError:
            pass  # Invalid date, ignore filter

    if date_to:
        try:
            to_dt = datetime.fromisoformat(date_to)
            # Include the entire day
            to_dt = to_dt.replace(hour=23, minute=59, second=59)
            query = query.where(NewsItem.published_at <= to_dt)  # type: ignore[arg-type]
            count_query = count_query.where(NewsItem.published_at <= to_dt)  # type: ignore[arg-type]
        except ValueError:
            pass  # Invalid date, ignore filter

    # Get total count
    total = await db.scalar(count_query)
    total = total or 0

    # Apply pagination
    query = query.limit(limit).offset(offset)

    result = await db.execute(query)
    items = result.scalars().all()

    # Fetch related sources for display
    source_ids = {item.source_id for item in items}
    if source_ids:
        sources_result = await db.execute(
            select(NewsSource).where(NewsSource.id.in_(source_ids))  # type: ignore[union-attr, arg-type]
        )
        sources_map = {s.id: s for s in sources_result.scalars().all()}
    else:
        sources_map = {}

    # Fetch related players for display
    player_ids = {item.player_id for item in items if item.player_id}
    if player_ids:
        players_result = await db.execute(
            select(PlayerMaster).where(PlayerMaster.id.in_(player_ids))  # type: ignore[union-attr, arg-type]
        )
        players_map = {p.id: p for p in players_result.scalars().all()}
    else:
        players_map = {}

    # Fetch all sources for filter dropdown
    all_sources_result = await db.execute(
        select(NewsSource).order_by(NewsSource.name)  # type: ignore[arg-type]
    )
    all_sources = all_sources_result.scalars().all()

    # Calculate pagination info
    pages = (total + limit - 1) // limit if total > 0 else 1
    current_page = (offset // limit) + 1

    success_messages = {
        "updated": "News item updated successfully.",
        "deleted": "News item deleted successfully.",
    }

    return request.app.state.templates.TemplateResponse(
        "admin/news-items/index.html",
        base_context(
            request,
            user=user,
            items=items,
            sources_map=sources_map,
            players_map=players_map,
            all_sources=all_sources,
            tags=list(NewsItemTag),
            total=total,
            limit=limit,
            offset=offset,
            pages=pages,
            current_page=current_page,
            source_id=source_id,
            tag=tag,
            date_from=date_from,
            date_to=date_to,
            success=success_messages.get(success) if success else None,
            active_nav="news-items",
        ),
    )


@router.get("/{item_id}", response_class=HTMLResponse)
async def edit_news_item(
    request: Request,
    item_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the edit news item form (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    result = await db.execute(
        select(NewsItem).where(NewsItem.id == item_id)  # type: ignore[arg-type]
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="News item not found")

    # Get the source for display
    source_result = await db.execute(
        select(NewsSource).where(NewsSource.id == item.source_id)  # type: ignore[arg-type]
    )
    source = source_result.scalar_one_or_none()

    # Get player if associated
    player = None
    if item.player_id:
        player_result = await db.execute(
            select(PlayerMaster).where(PlayerMaster.id == item.player_id)  # type: ignore[arg-type]
        )
        player = player_result.scalar_one_or_none()

    return request.app.state.templates.TemplateResponse(
        "admin/news-items/form.html",
        base_context(
            request,
            user=user,
            item=item,
            source=source,
            player=player,
            tags=list(NewsItemTag),
            error=None,
            active_nav="news-items",
        ),
    )


@router.post("/{item_id}", response_class=HTMLResponse)
async def update_news_item(
    request: Request,
    item_id: int,
    tag: str = Form(...),
    player_id: str | None = Form(default=None),
    summary: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Update a news item (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    async with db.begin():
        result = await db.execute(
            select(NewsItem).where(NewsItem.id == item_id)  # type: ignore[arg-type]
        )
        item = result.scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail="News item not found")

        # Validate tag
        try:
            tag_enum = NewsItemTag(tag)
        except ValueError:
            # Get source and player for re-rendering form
            source_result = await db.execute(
                select(NewsSource).where(NewsSource.id == item.source_id)  # type: ignore[arg-type]
            )
            source = source_result.scalar_one_or_none()

            player = None
            if item.player_id:
                player_result = await db.execute(
                    select(PlayerMaster).where(  # type: ignore[arg-type]
                        PlayerMaster.id == item.player_id
                    )
                )
                player = player_result.scalar_one_or_none()

            return request.app.state.templates.TemplateResponse(
                "admin/news-items/form.html",
                base_context(
                    request,
                    user=user,
                    item=item,
                    source=source,
                    player=player,
                    tags=list(NewsItemTag),
                    error=f"Invalid tag: {tag}",
                    active_nav="news-items",
                ),
            )

        # Parse player_id (may be empty string or None)
        parsed_player_id: int | None = None
        if player_id and player_id.strip():
            try:
                parsed_player_id = int(player_id.strip())
                # Validate player exists
                player_check = await db.execute(
                    select(PlayerMaster.id).where(  # type: ignore[call-overload]
                        PlayerMaster.id == parsed_player_id  # type: ignore[arg-type]
                    )
                )
                if player_check.scalar_one_or_none() is None:
                    parsed_player_id = None  # Invalid player ID, clear it
            except ValueError:
                parsed_player_id = None

        # Update fields
        item.tag = tag_enum
        item.player_id = parsed_player_id
        item.summary = summary.strip() if summary and summary.strip() else None

    return RedirectResponse(url="/admin/news-items?success=updated", status_code=303)


@router.post("/{item_id}/delete", response_class=HTMLResponse)
async def delete_news_item(
    request: Request,
    item_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete a news item (admin only)."""
    redirect, user = await require_admin(request, db)
    if redirect:
        return redirect

    async with db.begin():
        result = await db.execute(
            select(NewsItem).where(NewsItem.id == item_id)  # type: ignore[arg-type]
        )
        item = result.scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail="News item not found")

        await db.delete(item)

    return RedirectResponse(url="/admin/news-items?success=deleted", status_code=303)
