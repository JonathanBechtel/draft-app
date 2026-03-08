"""Admin YouTubeChannel CRUD routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.routes.admin.helpers import (
    base_context_with_permissions,
    require_dataset_access,
)
from app.schemas.player_content_mentions import ContentType, PlayerContentMention
from app.schemas.youtube_channels import YouTubeChannel
from app.schemas.youtube_videos import YouTubeVideo
from app.services.video_ingestion_service import run_ingestion_cycle
from app.utils.db_async import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/youtube-channels", tags=["admin-youtube-channels"])


@router.get("", response_class=HTMLResponse)
async def list_youtube_channels(
    request: Request,
    success: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """List all YouTube channels."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "youtube_channels",
        need_edit=False,
        next_path="/admin/youtube-channels",
    )
    if redirect:
        return redirect
    assert user is not None

    channels = (
        (
            await db.execute(select(YouTubeChannel).order_by(YouTubeChannel.name))  # type: ignore[arg-type]
        )
        .scalars()
        .all()
    )

    success_messages = {
        "created": "YouTube channel created successfully.",
        "updated": "YouTube channel updated successfully.",
        "deleted": "YouTube channel deleted successfully.",
        "ingested": "Video ingestion complete.",
    }
    if success == "ingested":
        parts = []
        added = request.query_params.get("added", "0")
        channels_count = request.query_params.get("channels", "0")
        filtered = request.query_params.get("filtered", "0")
        mentions = request.query_params.get("mentions", "0")
        parts.append(f"{added} videos added from {channels_count} channels")
        if filtered != "0":
            parts.append(f"{filtered} filtered")
        if mentions != "0":
            parts.append(f"{mentions} mentions")
        success_messages["ingested"] = f"Ingestion complete: {', '.join(parts)}."

    return request.app.state.templates.TemplateResponse(
        "admin/youtube-channels/index.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            channels=channels,
            success=success_messages.get(success) if success else None,
            error=error,
            active_nav="youtube-channels",
        ),
    )


@router.post("/ingest", response_class=HTMLResponse)
async def ingest_youtube_channels(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Trigger YouTube ingestion from admin UI."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "youtube_ingestion",
        need_edit=True,
        next_path="/admin/youtube-channels",
    )
    if redirect:
        return redirect
    assert user is not None

    try:
        result = await run_ingestion_cycle(db)
        return RedirectResponse(
            url=(
                f"/admin/youtube-channels?success=ingested"
                f"&added={result.videos_added}"
                f"&channels={result.channels_processed}"
                f"&filtered={result.videos_filtered}"
                f"&mentions={result.mentions_added}"
            ),
            status_code=303,
        )
    except Exception:
        logger.exception("YouTube ingestion failed")
        return RedirectResponse(
            url="/admin/youtube-channels?error=Ingestion+failed.+Check+server+logs.",
            status_code=303,
        )


@router.get("/new", response_class=HTMLResponse)
async def new_youtube_channel(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display create channel form."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "youtube_channels",
        need_edit=True,
        next_path="/admin/youtube-channels/new",
    )
    if redirect:
        return redirect
    assert user is not None

    return request.app.state.templates.TemplateResponse(
        "admin/youtube-channels/form.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            channel=None,
            error=None,
            active_nav="youtube-channels",
        ),
    )


@router.post("", response_class=HTMLResponse)
async def create_youtube_channel(
    request: Request,
    name: str = Form(...),
    display_name: str = Form(...),
    channel_id: str = Form(...),
    channel_url: str | None = Form(default=None),
    thumbnail_url: str | None = Form(default=None),
    description: str | None = Form(default=None),
    is_draft_focused: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    fetch_interval_minutes: int = Form(default=60),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Create a YouTube channel."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "youtube_channels",
        need_edit=True,
        next_path="/admin/youtube-channels",
    )
    if redirect:
        return redirect
    assert user is not None

    normalized_channel_id = channel_id.strip()
    if not normalized_channel_id:
        return request.app.state.templates.TemplateResponse(
            "admin/youtube-channels/form.html",
            await base_context_with_permissions(
                request,
                db,
                user,
                channel=None,
                error="Channel ID cannot be empty.",
                active_nav="youtube-channels",
            ),
        )

    async with db.begin():
        existing = await db.execute(
            select(YouTubeChannel).where(
                YouTubeChannel.channel_id == normalized_channel_id  # type: ignore[arg-type]
            )
        )
        if existing.scalar_one_or_none():
            return request.app.state.templates.TemplateResponse(
                "admin/youtube-channels/form.html",
                await base_context_with_permissions(
                    request,
                    db,
                    user,
                    channel=None,
                    error="A YouTube channel with this channel ID already exists.",
                    active_nav="youtube-channels",
                ),
            )

        now = datetime.now(UTC).replace(tzinfo=None)
        channel = YouTubeChannel(
            name=name,
            display_name=display_name,
            channel_id=normalized_channel_id,
            channel_url=channel_url or None,
            thumbnail_url=thumbnail_url or None,
            description=description or None,
            is_draft_focused=is_draft_focused is not None
            and is_draft_focused not in {"0", "", "false", "False"},
            is_active=is_active is not None
            and is_active not in {"0", "", "false", "False"},
            fetch_interval_minutes=fetch_interval_minutes,
            created_at=now,
            updated_at=now,
        )
        db.add(channel)

    return RedirectResponse(
        url="/admin/youtube-channels?success=created", status_code=303
    )


@router.get("/{channel_id}", response_class=HTMLResponse)
async def edit_youtube_channel(
    request: Request,
    channel_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Display edit channel form."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "youtube_channels",
        need_edit=False,
        next_path=f"/admin/youtube-channels/{channel_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    channel = (
        await db.execute(
            select(YouTubeChannel).where(YouTubeChannel.id == channel_id)  # type: ignore[arg-type]
        )
    ).scalar_one_or_none()
    if channel is None:
        raise HTTPException(status_code=404, detail="YouTube channel not found")

    return request.app.state.templates.TemplateResponse(
        "admin/youtube-channels/form.html",
        await base_context_with_permissions(
            request,
            db,
            user,
            channel=channel,
            error=None,
            active_nav="youtube-channels",
        ),
    )


@router.post("/{channel_id}", response_class=HTMLResponse)
async def update_youtube_channel(
    request: Request,
    channel_id: int,
    name: str = Form(...),
    display_name: str = Form(...),
    channel_key: str = Form(...),
    channel_url: str | None = Form(default=None),
    thumbnail_url: str | None = Form(default=None),
    description: str | None = Form(default=None),
    is_draft_focused: str | None = Form(default=None),
    is_active: str | None = Form(default=None),
    fetch_interval_minutes: int = Form(default=60),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Update a YouTube channel."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "youtube_channels",
        need_edit=True,
        next_path=f"/admin/youtube-channels/{channel_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    normalized_channel_key = channel_key.strip()
    if not normalized_channel_key:
        return request.app.state.templates.TemplateResponse(
            "admin/youtube-channels/form.html",
            await base_context_with_permissions(
                request,
                db,
                user,
                channel=None,
                error="Channel ID cannot be empty.",
                active_nav="youtube-channels",
            ),
        )

    async with db.begin():
        channel = (
            await db.execute(
                select(YouTubeChannel).where(YouTubeChannel.id == channel_id)  # type: ignore[arg-type]
            )
        ).scalar_one_or_none()
        if channel is None:
            raise HTTPException(status_code=404, detail="YouTube channel not found")

        existing = await db.execute(
            select(YouTubeChannel).where(
                YouTubeChannel.channel_id == normalized_channel_key,  # type: ignore[arg-type]
                YouTubeChannel.id != channel_id,  # type: ignore[arg-type]
            )
        )
        if existing.scalar_one_or_none():
            return request.app.state.templates.TemplateResponse(
                "admin/youtube-channels/form.html",
                await base_context_with_permissions(
                    request,
                    db,
                    user,
                    channel=channel,
                    error="A YouTube channel with this channel ID already exists.",
                    active_nav="youtube-channels",
                ),
            )

        channel.name = name
        channel.display_name = display_name
        channel.channel_id = normalized_channel_key
        channel.channel_url = channel_url or None
        channel.thumbnail_url = thumbnail_url or None
        channel.description = description or None
        channel.is_draft_focused = (
            is_draft_focused is not None
            and is_draft_focused
            not in {
                "0",
                "",
                "false",
                "False",
            }
        )
        channel.is_active = is_active is not None and is_active not in {
            "0",
            "",
            "false",
            "False",
        }
        channel.fetch_interval_minutes = fetch_interval_minutes
        channel.updated_at = datetime.now(UTC).replace(tzinfo=None)

    return RedirectResponse(
        url="/admin/youtube-channels?success=updated", status_code=303
    )


@router.post("/{channel_id}/delete", response_class=HTMLResponse)
async def delete_youtube_channel(
    request: Request,
    channel_id: int,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Delete a YouTube channel and associated videos."""
    redirect, user = await require_dataset_access(
        request,
        db,
        "youtube_channels",
        need_edit=True,
        next_path=f"/admin/youtube-channels/{channel_id}",
    )
    if redirect:
        return redirect
    assert user is not None

    async with db.begin():
        channel = (
            await db.execute(
                select(YouTubeChannel).where(YouTubeChannel.id == channel_id)  # type: ignore[arg-type]
            )
        ).scalar_one_or_none()
        if channel is None:
            raise HTTPException(status_code=404, detail="YouTube channel not found")

        channel_video_ids = (
            (
                await db.execute(
                    select(YouTubeVideo.__table__.c.id).where(  # type: ignore[attr-defined]
                        YouTubeVideo.channel_id == channel_id  # type: ignore[arg-type]
                    )
                )
            )
            .scalars()
            .all()
        )
        if channel_video_ids:
            await db.execute(
                delete(PlayerContentMention)
                .where(PlayerContentMention.content_type == ContentType.VIDEO)  # type: ignore[arg-type]
                .where(PlayerContentMention.content_id.in_(channel_video_ids))  # type: ignore[attr-defined]
            )
            await db.execute(
                delete(YouTubeVideo).where(YouTubeVideo.channel_id == channel_id)  # type: ignore[arg-type]
            )
        await db.delete(channel)

    return RedirectResponse(
        url="/admin/youtube-channels?success=deleted", status_code=303
    )
