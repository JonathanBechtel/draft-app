"""Admin NewsItem CRUD routes.

Provides read, update, and delete for news items. No create route since items
are ingested from RSS feeds.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import (
    base_context_with_permissions,
    require_dataset_access,
)
from app.schemas.news_items import NewsItem, NewsItemTag
from app.schemas.news_sources import NewsSource
from app.schemas.players_master import PlayerMaster
from app.services import board_extraction_service
from app.services.board_extraction_service import (
    BoardExtractionError,
    PaywallDetectedError,
)
from app.services.news_service import set_sticky_news_item
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
    source_id: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List all news items with pagination and filters."""
    redirect, user = await require_dataset_access(
        request, db, "news_ingestion", need_edit=False, next_path="/admin/news-items"
    )
    if redirect:
        return redirect
    assert user is not None  # Guaranteed by require_dataset_access if no redirect

    # Build base query
    query = select(NewsItem).order_by(NewsItem.published_at.desc())  # type: ignore[attr-defined]
    count_query = select(func.count(NewsItem.id))  # type: ignore[arg-type]

    # Apply filters
    # The filter form always submits source_id (possibly empty for "All Sources"),
    # so coerce defensively: blank or non-numeric values mean "no source filter".
    source_id_value: int | None = None
    if source_id:
        try:
            source_id_value = int(source_id)
        except ValueError:
            source_id_value = None

    if source_id_value is not None:
        query = query.where(NewsItem.source_id == source_id_value)  # type: ignore[arg-type]
        count_query = count_query.where(NewsItem.source_id == source_id_value)  # type: ignore[arg-type]

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
        await base_context_with_permissions(
            request,
            db,
            user,
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
            source_id=source_id_value,
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
    error: str | None = Query(default=None),
    success: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the edit news item form."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "news_ingestion",
        need_edit=False,
        next_path=f"/admin/news-items/{item_id}",
    )
    if redirect:
        return redirect
    assert user is not None  # Guaranteed by require_dataset_access if no redirect

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
        await base_context_with_permissions(
            request,
            db,
            user,
            item=item,
            source=source,
            player=player,
            tags=list(NewsItemTag),
            error=error,
            success=success,
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
    is_sticky: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Update a news item."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "news_ingestion",
        need_edit=True,
        next_path=f"/admin/news-items/{item_id}",
    )
    if redirect:
        return redirect
    assert user is not None  # Guaranteed by require_dataset_access if no redirect

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
                    select(PlayerMaster).where(
                        PlayerMaster.id == item.player_id  # type: ignore[arg-type]
                    )
                )
                player = player_result.scalar_one_or_none()

            return request.app.state.templates.TemplateResponse(
                "admin/news-items/form.html",
                await base_context_with_permissions(
                    request,
                    db,
                    user,
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

        # Sticky toggle: HTML form posts the field only when checked.
        # set_sticky_news_item enforces the single-sticky invariant.
        wants_sticky = is_sticky is not None and is_sticky.lower() in (
            "on",
            "true",
            "1",
        )
        if wants_sticky:
            await set_sticky_news_item(db, item_id)
        elif item.is_sticky:
            await set_sticky_news_item(db, None)

    return RedirectResponse(url="/admin/news-items?success=updated", status_code=303)


@router.post("/{item_id}/delete", response_class=HTMLResponse)
async def delete_news_item(
    request: Request,
    item_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete a news item."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "news_ingestion",
        need_edit=True,
        next_path=f"/admin/news-items/{item_id}",
    )
    if redirect:
        return redirect
    assert user is not None  # Guaranteed by require_dataset_access if no redirect

    async with db.begin():
        result = await db.execute(
            select(NewsItem).where(NewsItem.id == item_id)  # type: ignore[arg-type]
        )
        item = result.scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail="News item not found")

        await db.delete(item)

    return RedirectResponse(url="/admin/news-items?success=deleted", status_code=303)


@router.post("/{item_id}/extract-board", response_class=HTMLResponse)
async def extract_board_from_news_item(
    request: Request,
    item_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Trigger AI board extraction for a NewsItem and redirect to the result.

    Calls ``extract_board`` inline (network + Gemini), persists a PENDING
    Board, then redirects the admin to the board detail page so they can
    immediately review and resolve unresolved entries.

    Outcomes:
    - New board created → redirect to ``/admin/boards/{board_id}?success=extracted``.
    - Board already existed (duplicate) → redirect to the existing board
      with ``?success=already_extracted``.
    - No entries extracted → redirect back to this news item with an error notice.
    - ``PaywallDetectedError`` → redirect back with paywall error notice.
    - ``BoardExtractionError`` (incl. wrapped fetch failures) → redirect back
      with the error message.
    - Raw ``httpx.HTTPError`` → redirect back with a fetch-failure notice.
    """
    from app.schemas.boards import Board, BoardKind
    from sqlmodel import select as sm_select

    redirect, user = await require_dataset_access(
        request,
        db,
        "boards",
        need_edit=True,
        next_path=f"/admin/news-items/{item_id}",
    )
    if redirect:
        return redirect
    assert user is not None  # Guaranteed by require_dataset_access if no redirect

    def _back_with_error(message: str) -> Response:
        return RedirectResponse(
            url=f"/admin/news-items/{item_id}?error={quote(message)}",
            status_code=303,
        )

    # All reads and the extraction write happen inside a single transaction.
    # Reads prior to begin() would autobegin an implicit transaction and cause
    # begin() to raise InvalidRequestError (require_dataset_access already
    # commits its own auth transaction, so _transaction is None here).
    try:
        async with db.begin():
            # Verify the NewsItem exists (404 guard).
            item_result = await db.execute(
                select(NewsItem).where(NewsItem.id == item_id)  # type: ignore[arg-type]
            )
            item = item_result.scalar_one_or_none()
            if item is None:
                raise HTTPException(status_code=404, detail="News item not found")

            # Derive board kind from the item's tag so a MOCK_DRAFT article is
            # extracted and stored as a mock (correct provenance + dedup key),
            # not silently under the default BIG_BOARD. Extraction itself is
            # identical for both; kind only affects persistence/dedup.
            board_kind = (
                BoardKind.MOCK_DRAFT
                if item.tag == NewsItemTag.MOCK_DRAFT
                else BoardKind.BIG_BOARD
            )

            # Snapshot whether a board already existed so we can distinguish
            # "just created" from "already there" without a second DB round-trip.
            pre_stmt = (
                sm_select(Board)
                .where(Board.news_item_id == item_id)  # type: ignore[arg-type]
                .where(Board.kind == board_kind)  # type: ignore[arg-type]
                .limit(1)
            )
            pre_result = await db.execute(pre_stmt)
            pre_existing_board = pre_result.scalar_one_or_none()

            board = await board_extraction_service.extract_board(
                db, news_item_id=item_id, kind=board_kind
            )
    except HTTPException:
        raise
    except PaywallDetectedError:
        return _back_with_error(
            "Article is paywalled — extraction cannot proceed. "
            "Only free articles can be extracted."
        )
    except BoardExtractionError as exc:
        return _back_with_error(str(exc))
    except httpx.HTTPError as exc:
        # Defensive: the default fetcher wraps transport errors in
        # BoardExtractionError, but a custom fetcher might not — never 500.
        return _back_with_error(f"Could not fetch the article: {exc}")

    if board is None:
        # Gemini returned no ranked entries — nothing to review.
        return _back_with_error(
            "No ranked entries were found in this article. "
            "The article may not contain a board, or extraction failed to "
            "identify any players."
        )

    # Determine whether the board is new or was already there.
    was_duplicate = pre_existing_board is not None and pre_existing_board.id == board.id
    success_key = "already_extracted" if was_duplicate else "extracted"
    return RedirectResponse(
        url=f"/admin/boards/{board.id}?success={success_key}",
        status_code=303,
    )
