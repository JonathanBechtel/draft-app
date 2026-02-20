"""Admin PodcastShow CRUD routes."""

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
from app.schemas.podcast_episodes import PodcastEpisode
from app.schemas.podcast_shows import PodcastShow
from app.utils.db_async import get_session

router = APIRouter(prefix="/podcast-shows", tags=["admin-podcast-shows"])


@router.get("", response_class=HTMLResponse)
async def list_podcast_shows(
    request: Request,
    success: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List all podcast shows."""
    redirect, user = await require_dataset_access(
        request, db, "podcasts", need_edit=False, next_path="/admin/podcast-shows"
    )
    if redirect:
        return redirect
    assert user is not None

    result = await db.execute(
        select(PodcastShow).order_by(PodcastShow.name)  # type: ignore[arg-type]
    )
    shows = result.scalars().all()

    success_messages = {
        "created": "Podcast show created successfully.",
        "updated": "Podcast show updated successfully.",
        "deleted": "Podcast show deleted successfully.",
    }

    return request.app.state.templates.TemplateResponse(
        "admin/podcast-shows/index.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            shows=shows,
            success=success_messages.get(success) if success else None,
        ),
    )


@router.get("/new", response_class=HTMLResponse)
async def new_podcast_show(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the create podcast show form."""
    redirect, user = await require_dataset_access(
        request, db, "podcasts", need_edit=True, next_path="/admin/podcast-shows/new"
    )
    if redirect:
        return redirect
    assert user is not None

    return request.app.state.templates.TemplateResponse(
        "admin/podcast-shows/form.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            show=None,
            error=None,
        ),
    )


@router.post("", response_class=HTMLResponse)
async def create_podcast_show(
    request: Request,
    name: str = Form(...),
    display_name: str = Form(...),
    feed_url: str = Form(...),
    artwork_url: str | None = Form(default=None),
    author: str | None = Form(default=None),
    description: str | None = Form(default=None),
    website_url: str | None = Form(default=None),
    is_draft_focused: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    fetch_interval_minutes: int = Form(default=30),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Create a new podcast show."""
    redirect, user = await require_dataset_access(
        request, db, "podcasts", need_edit=True, next_path="/admin/podcast-shows"
    )
    if redirect:
        return redirect
    assert user is not None

    async with db.begin():
        # Check for duplicate feed_url
        existing = await db.execute(
            select(PodcastShow).where(
                PodcastShow.feed_url == feed_url  # type: ignore[arg-type]
            )
        )
        if existing.scalar_one_or_none():
            return request.app.state.templates.TemplateResponse(
                "admin/podcast-shows/form.html",
                await base_context_with_permissions(
                    request,
                    db,
                    user,
                    show=None,
                    error="A podcast show with this feed URL already exists.",
                ),
            )

        now = datetime.now(UTC).replace(tzinfo=None)
        show = PodcastShow(
            name=name,
            display_name=display_name,
            feed_url=feed_url,
            artwork_url=artwork_url or None,
            author=author or None,
            description=description or None,
            website_url=website_url or None,
            is_draft_focused=is_draft_focused is not None
            and is_draft_focused not in {"0", "", "false", "False"},
            is_active=is_active is not None
            and is_active not in {"0", "", "false", "False"},
            fetch_interval_minutes=fetch_interval_minutes,
            created_at=now,
            updated_at=now,
        )
        db.add(show)

    return RedirectResponse(url="/admin/podcast-shows?success=created", status_code=303)


@router.get("/{show_id}", response_class=HTMLResponse)
async def edit_podcast_show(
    request: Request,
    show_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the edit podcast show form."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "podcasts",
        need_edit=False,
        next_path=f"/admin/podcast-shows/{show_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    result = await db.execute(
        select(PodcastShow).where(PodcastShow.id == show_id)  # type: ignore[arg-type]
    )
    show = result.scalar_one_or_none()
    if show is None:
        raise HTTPException(status_code=404, detail="Podcast show not found")

    return request.app.state.templates.TemplateResponse(
        "admin/podcast-shows/form.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            show=show,
            error=None,
        ),
    )


@router.post("/{show_id}", response_class=HTMLResponse)
async def update_podcast_show(
    request: Request,
    show_id: int,
    name: str = Form(...),
    display_name: str = Form(...),
    feed_url: str = Form(...),
    artwork_url: str | None = Form(default=None),
    author: str | None = Form(default=None),
    description: str | None = Form(default=None),
    website_url: str | None = Form(default=None),
    is_draft_focused: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    fetch_interval_minutes: int = Form(default=30),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Update a podcast show."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "podcasts",
        need_edit=True,
        next_path=f"/admin/podcast-shows/{show_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    async with db.begin():
        result = await db.execute(
            select(PodcastShow).where(
                PodcastShow.id == show_id  # type: ignore[arg-type]
            )
        )
        show = result.scalar_one_or_none()
        if show is None:
            raise HTTPException(status_code=404, detail="Podcast show not found")

        # Check for duplicate feed_url (exclude current show)
        existing = await db.execute(
            select(PodcastShow).where(
                PodcastShow.feed_url == feed_url,  # type: ignore[arg-type]
                PodcastShow.id != show_id,  # type: ignore[arg-type]
            )
        )
        if existing.scalar_one_or_none():
            return request.app.state.templates.TemplateResponse(
                "admin/podcast-shows/form.html",
                await base_context_with_permissions(
                    request,
                    db,
                    user,
                    show=show,
                    error="A podcast show with this feed URL already exists.",
                ),
            )

        show.name = name
        show.display_name = display_name
        show.feed_url = feed_url
        show.artwork_url = artwork_url or None
        show.author = author or None
        show.description = description or None
        show.website_url = website_url or None
        show.is_draft_focused = (
            is_draft_focused is not None
            and is_draft_focused
            not in {
                "0",
                "",
                "false",
                "False",
            }
        )
        show.is_active = is_active is not None and is_active not in {
            "0",
            "",
            "false",
            "False",
        }
        show.fetch_interval_minutes = fetch_interval_minutes
        show.updated_at = datetime.now(UTC).replace(tzinfo=None)

    return RedirectResponse(url="/admin/podcast-shows?success=updated", status_code=303)


@router.post("/{show_id}/delete", response_class=HTMLResponse)
async def delete_podcast_show(
    request: Request,
    show_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete a podcast show."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "podcasts",
        need_edit=True,
        next_path=f"/admin/podcast-shows/{show_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    async with db.begin():
        result = await db.execute(
            select(PodcastShow).where(
                PodcastShow.id == show_id  # type: ignore[arg-type]
            )
        )
        show = result.scalar_one_or_none()
        if show is None:
            raise HTTPException(status_code=404, detail="Podcast show not found")

        # Check for dependent episodes
        episodes_count_result = await db.execute(
            select(func.count()).where(
                PodcastEpisode.show_id == show_id  # type: ignore[arg-type]
            )
        )
        episodes_count = episodes_count_result.scalar_one()

        if episodes_count > 0:
            sources_result = await db.execute(
                select(PodcastShow).order_by(PodcastShow.name)  # type: ignore[arg-type]
            )
            shows = sources_result.scalars().all()

            return request.app.state.templates.TemplateResponse(
                "admin/podcast-shows/index.html",
                await base_context_with_permissions(
                    request,
                    db,
                    user,
                    shows=shows,
                    error=f"Cannot delete '{show.name}': it has {episodes_count} associated "
                    "episode(s). Deactivate it instead or delete the episodes first.",
                    success=None,
                ),
            )

        await db.delete(show)

    return RedirectResponse(url="/admin/podcast-shows?success=deleted", status_code=303)
