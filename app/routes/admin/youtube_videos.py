"""Admin YouTubeVideo CRUD routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import (
    base_context_with_permissions,
    require_dataset_access,
)
from app.schemas.player_content_mentions import (
    ContentType,
    MentionSource,
    PlayerContentMention,
)
from app.schemas.players_master import PlayerMaster
from app.schemas.youtube_channels import YouTubeChannel
from app.schemas.youtube_videos import YouTubeVideo, YouTubeVideoTag
from app.services.video_ingestion_service import (
    add_video_by_url,
    reconcile_manual_mentions,
)
from app.services.video_service import coerce_video_tag
from app.utils.db_async import get_session

router = APIRouter(prefix="/youtube-videos", tags=["admin-youtube-videos"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


@router.get("", response_class=HTMLResponse)
async def list_youtube_videos(
    request: Request,
    success: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    channel_id: int | None = Query(default=None),
    tag: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List film-room videos with pagination and filters."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "youtube_videos",
        need_edit=False,
        next_path="/admin/youtube-videos",
    )
    if redirect:
        return redirect
    assert user is not None

    query = select(YouTubeVideo).order_by(YouTubeVideo.published_at.desc())  # type: ignore[attr-defined]
    count_query = select(func.count(YouTubeVideo.id))  # type: ignore[arg-type]

    if channel_id is not None:
        query = query.where(YouTubeVideo.channel_id == channel_id)  # type: ignore[arg-type]
        count_query = count_query.where(YouTubeVideo.channel_id == channel_id)  # type: ignore[arg-type]

    if tag:
        tag_enum = coerce_video_tag(tag)
        if tag_enum:
            query = query.where(YouTubeVideo.tag == tag_enum)  # type: ignore[arg-type]
            count_query = count_query.where(YouTubeVideo.tag == tag_enum)  # type: ignore[arg-type]

    total = int((await db.scalar(count_query)) or 0)
    videos = (await db.execute(query.limit(limit).offset(offset))).scalars().all()

    channel_ids = {video.channel_id for video in videos}
    channels_map: dict[int, YouTubeChannel] = {}
    if channel_ids:
        channels_map = {
            c.id: c
            for c in (
                await db.execute(
                    select(YouTubeChannel).where(YouTubeChannel.id.in_(channel_ids))  # type: ignore[union-attr,arg-type]
                )
            )
            .scalars()
            .all()
            if c.id is not None
        }

    all_channels = (
        (
            await db.execute(select(YouTubeChannel).order_by(YouTubeChannel.name))  # type: ignore[arg-type]
        )
        .scalars()
        .all()
    )

    pages = (total + limit - 1) // limit if total > 0 else 1
    current_page = (offset // limit) + 1
    success_messages = {
        "updated": "Video updated successfully.",
        "deleted": "Video deleted successfully.",
        "added": "Video added successfully.",
    }

    return request.app.state.templates.TemplateResponse(
        "admin/youtube-videos/index.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            videos=videos,
            channels_map=channels_map,
            all_channels=all_channels,
            tags=list(YouTubeVideoTag),
            total=total,
            limit=limit,
            offset=offset,
            pages=pages,
            current_page=current_page,
            channel_id=channel_id,
            tag=tag,
            success=success_messages.get(success) if success else None,
            active_nav="youtube-videos",
        ),
    )


@router.get("/add", response_class=HTMLResponse)
async def add_youtube_video_form(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display manual add form for YouTube video URL."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "youtube_videos",
        need_edit=True,
        next_path="/admin/youtube-videos/add",
    )
    if redirect:
        return redirect
    assert user is not None

    return request.app.state.templates.TemplateResponse(
        "admin/youtube-videos/add.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            tags=list(YouTubeVideoTag),
            error=None,
            active_nav="youtube-videos",
        ),
    )


@router.post("/add", response_class=HTMLResponse)
async def add_youtube_video(
    request: Request,
    youtube_url: str = Form(...),
    tag: str | None = Form(default=None),
    player_ids: str | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Add a new video by URL and optional manual player IDs."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "youtube_videos",
        need_edit=True,
        next_path="/admin/youtube-videos/add",
    )
    if redirect:
        return redirect
    assert user is not None

    parsed_player_ids = _parse_player_ids_csv(player_ids)
    try:
        await add_video_by_url(
            db=db,
            youtube_url=youtube_url,
            tag=tag,
            player_ids=parsed_player_ids,
        )
    except Exception as exc:
        return request.app.state.templates.TemplateResponse(
            "admin/youtube-videos/add.html",
            await base_context_with_permissions(
                request,
                db,
                user,
                tags=list(YouTubeVideoTag),
                error=str(exc),
                active_nav="youtube-videos",
            ),
        )

    return RedirectResponse(url="/admin/youtube-videos?success=added", status_code=303)


@router.get("/{video_id}", response_class=HTMLResponse)
async def edit_youtube_video(
    request: Request,
    video_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display video edit form."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "youtube_videos",
        need_edit=False,
        next_path=f"/admin/youtube-videos/{video_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    video = (
        await db.execute(
            select(YouTubeVideo).where(YouTubeVideo.id == video_id)  # type: ignore[arg-type]
        )
    ).scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=404, detail="Video not found")

    channel = (
        await db.execute(
            select(YouTubeChannel).where(YouTubeChannel.id == video.channel_id)  # type: ignore[arg-type]
        )
    ).scalar_one_or_none()
    mention_rows = (
        (
            await db.execute(
                select(PlayerContentMention.__table__.c.player_id).where(  # type: ignore[attr-defined]
                    PlayerContentMention.content_type == ContentType.VIDEO,  # type: ignore[arg-type]
                    PlayerContentMention.content_id == video_id,  # type: ignore[arg-type]
                    PlayerContentMention.source == MentionSource.MANUAL,  # type: ignore[arg-type]
                )
            )
        )
        .scalars()
        .all()
    )
    selected_player_ids = {int(pid) for pid in mention_rows}
    players = (
        (
            await db.execute(
                select(PlayerMaster).order_by(PlayerMaster.display_name)  # type: ignore[arg-type]
            )
        )
        .scalars()
        .all()
    )

    return request.app.state.templates.TemplateResponse(
        "admin/youtube-videos/form.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            video=video,
            channel=channel,
            tags=list(YouTubeVideoTag),
            players=players,
            selected_player_ids=selected_player_ids,
            error=None,
            active_nav="youtube-videos",
        ),
    )


@router.post("/{video_id}", response_class=HTMLResponse)
async def update_youtube_video(
    request: Request,
    video_id: int,
    title: str = Form(...),
    summary: str | None = Form(default=None),
    tag: str = Form(...),
    player_ids: list[str] | None = Form(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Update a video and reconcile MANUAL mentions additively."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "youtube_videos",
        need_edit=True,
        next_path=f"/admin/youtube-videos/{video_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    tag_enum = coerce_video_tag(tag)
    if tag_enum is None:
        raise HTTPException(status_code=400, detail=f"Invalid tag: {tag}")

    parsed_player_ids = [int(value) for value in (player_ids or []) if value.isdigit()]
    try:
        async with db.begin():
            video = (
                await db.execute(
                    select(YouTubeVideo).where(YouTubeVideo.id == video_id)  # type: ignore[arg-type]
                )
            ).scalar_one_or_none()
            if video is None:
                raise HTTPException(status_code=404, detail="Video not found")

            video.title = title
            video.summary = summary.strip() if summary and summary.strip() else None
            video.tag = tag_enum
            video.is_manually_added = True

            await reconcile_manual_mentions(
                db,
                video_id=video_id,
                player_ids=parsed_player_ids,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(
        url="/admin/youtube-videos?success=updated", status_code=303
    )


@router.post("/{video_id}/delete", response_class=HTMLResponse)
async def delete_youtube_video(
    request: Request,
    video_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete a video and all related mention rows."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "youtube_videos",
        need_edit=True,
        next_path=f"/admin/youtube-videos/{video_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    async with db.begin():
        video = (
            await db.execute(
                select(YouTubeVideo).where(YouTubeVideo.id == video_id)  # type: ignore[arg-type]
            )
        ).scalar_one_or_none()
        if video is None:
            raise HTTPException(status_code=404, detail="Video not found")

        await db.execute(
            delete(PlayerContentMention)
            .where(PlayerContentMention.content_type == ContentType.VIDEO)  # type: ignore[arg-type]
            .where(PlayerContentMention.content_id == video_id)  # type: ignore[arg-type]
        )
        await db.delete(video)

    return RedirectResponse(
        url="/admin/youtube-videos?success=deleted", status_code=303
    )


def _parse_player_ids_csv(raw: str | None) -> list[int]:
    if not raw:
        return []
    result: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.isdigit():
            result.append(int(chunk))
    return sorted(set(result))
