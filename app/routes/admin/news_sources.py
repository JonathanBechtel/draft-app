"""Admin NewsSource CRUD routes."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import (
    base_context_with_permissions,
    require_dataset_access,
)
from app.schemas.news_items import NewsItem
from app.schemas.news_sources import FeedType, NewsSource
from app.utils.db_async import get_session

router = APIRouter(prefix="/news-sources", tags=["admin-news-sources"])


@router.get("", response_class=HTMLResponse)
async def list_news_sources(
    request: Request,
    success: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List all news sources."""
    redirect, user = await require_dataset_access(
        request, db, "news_sources", need_edit=False, next_path="/admin/news-sources"
    )
    if redirect:
        return redirect
    assert user is not None  # Guaranteed by require_dataset_access if no redirect

    result = await db.execute(
        select(NewsSource).order_by(NewsSource.name)  # type: ignore[arg-type]
    )
    sources = result.scalars().all()

    success_messages = {
        "created": "News source created successfully.",
        "updated": "News source updated successfully.",
        "deleted": "News source deleted successfully.",
    }

    return request.app.state.templates.TemplateResponse(
        "admin/news-sources/index.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            sources=sources,
            success=success_messages.get(success) if success else None,
        ),
    )


@router.get("/new", response_class=HTMLResponse)
async def new_news_source(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the create news source form."""
    redirect, user = await require_dataset_access(
        request, db, "news_sources", need_edit=True, next_path="/admin/news-sources/new"
    )
    if redirect:
        return redirect
    assert user is not None  # Guaranteed by require_dataset_access if no redirect

    return request.app.state.templates.TemplateResponse(
        "admin/news-sources/form.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            source=None,
            feed_types=list(FeedType),
            error=None,
        ),
    )


@router.post("", response_class=HTMLResponse)
async def create_news_source(
    request: Request,
    name: str = Form(...),
    display_name: str = Form(...),
    feed_type: str = Form(...),
    feed_url: str = Form(...),
    is_active: str | None = Form(default=None),
    fetch_interval_minutes: int = Form(default=30),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Create a new news source."""
    redirect, user = await require_dataset_access(
        request, db, "news_sources", need_edit=True, next_path="/admin/news-sources"
    )
    if redirect:
        return redirect
    assert user is not None  # Guaranteed by require_dataset_access if no redirect

    # Validate feed_type
    try:
        feed_type_enum = FeedType(feed_type)
    except ValueError:
        return request.app.state.templates.TemplateResponse(
            "admin/news-sources/form.html",
            await base_context_with_permissions(
                request,
                db,
                user,
                source=None,
                feed_types=list(FeedType),
                error=f"Invalid feed type: {feed_type}",
            ),
        )

    async with db.begin():
        # Check for duplicate feed_url
        existing = await db.execute(
            select(NewsSource).where(
                NewsSource.feed_url == feed_url  # type: ignore[arg-type]
            )
        )
        if existing.scalar_one_or_none():
            return request.app.state.templates.TemplateResponse(
                "admin/news-sources/form.html",
                await base_context_with_permissions(
                    request,
                    db,
                    user,
                    source=None,
                    feed_types=list(FeedType),
                    error="A news source with this feed URL already exists.",
                ),
            )

        source = NewsSource(
            name=name,
            display_name=display_name,
            feed_type=feed_type_enum,
            feed_url=feed_url,
            is_active=is_active is not None
            and is_active not in {"0", "", "false", "False"},
            fetch_interval_minutes=fetch_interval_minutes,
        )
        db.add(source)

    return RedirectResponse(url="/admin/news-sources?success=created", status_code=303)


@router.get("/{source_id}", response_class=HTMLResponse)
async def edit_news_source(
    request: Request,
    source_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the edit news source form."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "news_sources",
        need_edit=False,
        next_path=f"/admin/news-sources/{source_id}",
    )
    if redirect:
        return redirect
    assert user is not None  # Guaranteed by require_dataset_access if no redirect

    result = await db.execute(
        select(NewsSource).where(NewsSource.id == source_id)  # type: ignore[arg-type]
    )
    source = result.scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="News source not found")

    return request.app.state.templates.TemplateResponse(
        "admin/news-sources/form.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            source=source,
            feed_types=list(FeedType),
            error=None,
        ),
    )


@router.post("/{source_id}", response_class=HTMLResponse)
async def update_news_source(
    request: Request,
    source_id: int,
    name: str = Form(...),
    display_name: str = Form(...),
    feed_type: str = Form(...),
    feed_url: str = Form(...),
    is_active: str | None = Form(default=None),
    fetch_interval_minutes: int = Form(default=30),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Update a news source."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "news_sources",
        need_edit=True,
        next_path=f"/admin/news-sources/{source_id}",
    )
    if redirect:
        return redirect
    assert user is not None  # Guaranteed by require_dataset_access if no redirect

    async with db.begin():
        result = await db.execute(
            select(NewsSource).where(
                NewsSource.id == source_id  # type: ignore[arg-type]
            )
        )
        source = result.scalar_one_or_none()
        if source is None:
            raise HTTPException(status_code=404, detail="News source not found")

        # Validate feed_type
        try:
            feed_type_enum = FeedType(feed_type)
        except ValueError:
            return request.app.state.templates.TemplateResponse(
                "admin/news-sources/form.html",
                await base_context_with_permissions(
                    request,
                    db,
                    user,
                    source=source,
                    feed_types=list(FeedType),
                    error=f"Invalid feed type: {feed_type}",
                ),
            )

        # Check for duplicate feed_url (exclude current source)
        existing = await db.execute(
            select(NewsSource).where(
                NewsSource.feed_url == feed_url,  # type: ignore[arg-type]
                NewsSource.id != source_id,  # type: ignore[arg-type]
            )
        )
        if existing.scalar_one_or_none():
            return request.app.state.templates.TemplateResponse(
                "admin/news-sources/form.html",
                await base_context_with_permissions(
                    request,
                    db,
                    user,
                    source=source,
                    feed_types=list(FeedType),
                    error="A news source with this feed URL already exists.",
                ),
            )

        source.name = name
        source.display_name = display_name
        source.feed_type = feed_type_enum
        source.feed_url = feed_url
        source.is_active = is_active is not None and is_active not in {
            "0",
            "",
            "false",
            "False",
        }
        source.fetch_interval_minutes = fetch_interval_minutes
        source.updated_at = datetime.now(UTC).replace(tzinfo=None)

    return RedirectResponse(url="/admin/news-sources?success=updated", status_code=303)


@router.post("/{source_id}/delete", response_class=HTMLResponse)
async def delete_news_source(
    request: Request,
    source_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete a news source."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "news_sources",
        need_edit=True,
        next_path=f"/admin/news-sources/{source_id}",
    )
    if redirect:
        return redirect
    assert user is not None  # Guaranteed by require_dataset_access if no redirect

    async with db.begin():
        result = await db.execute(
            select(NewsSource).where(
                NewsSource.id == source_id  # type: ignore[arg-type]
            )
        )
        source = result.scalar_one_or_none()
        if source is None:
            raise HTTPException(status_code=404, detail="News source not found")

        # Check for dependent news items
        items_count_result = await db.execute(
            select(func.count()).where(
                NewsItem.source_id == source_id  # type: ignore[arg-type]
            )
        )
        items_count = items_count_result.scalar_one()

        if items_count > 0:
            # Re-fetch sources for the list view
            sources_result = await db.execute(
                select(NewsSource).order_by(NewsSource.name)  # type: ignore[arg-type]
            )
            sources = sources_result.scalars().all()

            return request.app.state.templates.TemplateResponse(
                "admin/news-sources/index.html",
                await base_context_with_permissions(
                    request,
                    db,
                    user,
                    sources=sources,
                    error=f"Cannot delete '{source.name}': it has {items_count} associated "
                    "news item(s). Deactivate it instead or delete the news items first.",
                    success=None,
                ),
            )

        await db.delete(source)

    return RedirectResponse(url="/admin/news-sources?success=deleted", status_code=303)
