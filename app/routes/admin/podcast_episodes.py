"""Admin PodcastEpisode CRUD routes.

Provides read, update, and delete for podcast episodes. No create route since
episodes are ingested from RSS feeds.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import (
    base_context_with_permissions,
    require_dataset_access,
)
from app.schemas.podcast_episodes import PodcastEpisode, PodcastEpisodeTag
from app.schemas.podcast_shows import PodcastShow
from app.schemas.players_master import PlayerMaster
from app.utils.db_async import get_session

router = APIRouter(prefix="/podcast-episodes", tags=["admin-podcast-episodes"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


@router.get("", response_class=HTMLResponse)
async def list_podcast_episodes(
    request: Request,
    success: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    show_id: int | None = Query(default=None),
    tag: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List all podcast episodes with pagination and filters."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "podcast_ingestion",
        need_edit=False,
        next_path="/admin/podcast-episodes",
    )
    if redirect:
        return redirect
    assert user is not None

    # Build base query
    query = select(PodcastEpisode).order_by(PodcastEpisode.published_at.desc())  # type: ignore[attr-defined]
    count_query = select(func.count(PodcastEpisode.id))  # type: ignore[arg-type]

    # Apply filters
    if show_id is not None:
        query = query.where(PodcastEpisode.show_id == show_id)  # type: ignore[arg-type]
        count_query = count_query.where(PodcastEpisode.show_id == show_id)  # type: ignore[arg-type]

    if tag:
        try:
            tag_enum = PodcastEpisodeTag(tag)
            query = query.where(PodcastEpisode.tag == tag_enum)  # type: ignore[arg-type]
            count_query = count_query.where(PodcastEpisode.tag == tag_enum)  # type: ignore[arg-type]
        except ValueError:
            pass

    # Get total count
    total = await db.scalar(count_query)
    total = total or 0

    # Apply pagination
    query = query.limit(limit).offset(offset)

    result = await db.execute(query)
    episodes = result.scalars().all()

    # Fetch related shows for display
    ep_show_ids = {ep.show_id for ep in episodes}
    if ep_show_ids:
        shows_result = await db.execute(
            select(PodcastShow).where(PodcastShow.id.in_(ep_show_ids))  # type: ignore[union-attr, arg-type]
        )
        shows_map = {s.id: s for s in shows_result.scalars().all()}
    else:
        shows_map = {}

    # Fetch all shows for filter dropdown
    all_shows_result = await db.execute(
        select(PodcastShow).order_by(PodcastShow.name)  # type: ignore[arg-type]
    )
    all_shows = all_shows_result.scalars().all()

    # Calculate pagination info
    pages = (total + limit - 1) // limit if total > 0 else 1
    current_page = (offset // limit) + 1

    success_messages = {
        "updated": "Podcast episode updated successfully.",
        "deleted": "Podcast episode deleted successfully.",
    }

    return request.app.state.templates.TemplateResponse(
        "admin/podcast-episodes/index.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            episodes=episodes,
            shows_map=shows_map,
            all_shows=all_shows,
            tags=list(PodcastEpisodeTag),
            total=total,
            limit=limit,
            offset=offset,
            pages=pages,
            current_page=current_page,
            show_id=show_id,
            tag=tag,
            success=success_messages.get(success) if success else None,
            active_nav="podcast-episodes",
        ),
    )


@router.get("/{episode_id}", response_class=HTMLResponse)
async def edit_podcast_episode(
    request: Request,
    episode_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display the edit podcast episode form."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "podcast_ingestion",
        need_edit=False,
        next_path=f"/admin/podcast-episodes/{episode_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    result = await db.execute(
        select(PodcastEpisode).where(PodcastEpisode.id == episode_id)  # type: ignore[arg-type]
    )
    episode = result.scalar_one_or_none()
    if episode is None:
        raise HTTPException(status_code=404, detail="Podcast episode not found")

    # Get the show for display
    show_result = await db.execute(
        select(PodcastShow).where(PodcastShow.id == episode.show_id)  # type: ignore[arg-type]
    )
    show = show_result.scalar_one_or_none()

    # Get player if associated
    player = None
    if episode.player_id:
        player_result = await db.execute(
            select(PlayerMaster).where(PlayerMaster.id == episode.player_id)  # type: ignore[arg-type]
        )
        player = player_result.scalar_one_or_none()

    return request.app.state.templates.TemplateResponse(
        "admin/podcast-episodes/form.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            episode=episode,
            show=show,
            player=player,
            tags=list(PodcastEpisodeTag),
            error=None,
            active_nav="podcast-episodes",
        ),
    )


@router.post("/{episode_id}", response_class=HTMLResponse)
async def update_podcast_episode(
    request: Request,
    episode_id: int,
    title: str = Form(...),
    summary: str | None = Form(default=None),
    tag: str = Form(...),
    audio_url: str = Form(...),
    episode_url: str | None = Form(default=None),
    player_id: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Update a podcast episode."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "podcast_ingestion",
        need_edit=True,
        next_path=f"/admin/podcast-episodes/{episode_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    async with db.begin():
        result = await db.execute(
            select(PodcastEpisode).where(PodcastEpisode.id == episode_id)  # type: ignore[arg-type]
        )
        episode = result.scalar_one_or_none()
        if episode is None:
            raise HTTPException(status_code=404, detail="Podcast episode not found")

        # Validate tag
        try:
            tag_enum = PodcastEpisodeTag(tag)
        except ValueError:
            show_result = await db.execute(
                select(PodcastShow).where(PodcastShow.id == episode.show_id)  # type: ignore[arg-type]
            )
            show = show_result.scalar_one_or_none()

            player = None
            if episode.player_id:
                player_result = await db.execute(
                    select(PlayerMaster).where(
                        PlayerMaster.id == episode.player_id  # type: ignore[arg-type]
                    )
                )
                player = player_result.scalar_one_or_none()

            return request.app.state.templates.TemplateResponse(
                "admin/podcast-episodes/form.html",
                await base_context_with_permissions(
                    request,
                    db,
                    user,
                    episode=episode,
                    show=show,
                    player=player,
                    tags=list(PodcastEpisodeTag),
                    error=f"Invalid tag: {tag}",
                    active_nav="podcast-episodes",
                ),
            )

        # Parse player_id
        parsed_player_id: int | None = None
        if player_id and player_id.strip():
            try:
                parsed_player_id = int(player_id.strip())
                player_check = await db.execute(
                    select(PlayerMaster.id).where(  # type: ignore[call-overload]
                        PlayerMaster.id == parsed_player_id  # type: ignore[arg-type]
                    )
                )
                if player_check.scalar_one_or_none() is None:
                    parsed_player_id = None
            except ValueError:
                parsed_player_id = None

        episode.title = title
        episode.summary = summary.strip() if summary and summary.strip() else None
        episode.tag = tag_enum
        episode.audio_url = audio_url
        episode.episode_url = episode_url or None
        episode.player_id = parsed_player_id

    return RedirectResponse(
        url="/admin/podcast-episodes?success=updated", status_code=303
    )


@router.post("/{episode_id}/delete", response_class=HTMLResponse)
async def delete_podcast_episode(
    request: Request,
    episode_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete a podcast episode."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "podcast_ingestion",
        need_edit=True,
        next_path=f"/admin/podcast-episodes/{episode_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    async with db.begin():
        result = await db.execute(
            select(PodcastEpisode).where(PodcastEpisode.id == episode_id)  # type: ignore[arg-type]
        )
        episode = result.scalar_one_or_none()
        if episode is None:
            raise HTTPException(status_code=404, detail="Podcast episode not found")

        await db.delete(episode)

    return RedirectResponse(
        url="/admin/podcast-episodes?success=deleted", status_code=303
    )
