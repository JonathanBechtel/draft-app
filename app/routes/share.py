"""Share routes for social previews (Open Graph / Twitter cards)."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from fastapi import APIRouter, HTTPException, Query, Request

from app.services.s3_client import s3_client
from app.services.share_cards.export_service import ComponentType
from app.services.share_cards.storage import get_export_storage

router = APIRouter(tags=["share"])

_EXPORT_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _sanitize_next(next_path: str | None) -> str | None:
    if not next_path:
        return None
    if len(next_path) > 2048:
        return None
    if not next_path.startswith("/"):
        return None
    if next_path.startswith("//"):
        return None
    if "://" in next_path:
        return None
    if any(ch in next_path for ch in ("\n", "\r", "\t")):
        return None
    return next_path


@router.get("/share/{component}/{export_id}")
async def share_card(
    request: Request,
    component: ComponentType,
    export_id: str,
    title: str | None = Query(default=None, max_length=140),
    next: str | None = Query(default=None, alias="next", max_length=2048),
):
    """Render a tiny HTML wrapper for social previews.

    X/Twitter will fetch this URL and render the share card image from meta tags.
    """
    if not _EXPORT_ID_RE.fullmatch(export_id):
        # Avoid making arbitrary paths fetchable (and keep links short/guessable).
        raise HTTPException(status_code=404, detail="Share card not found")

    cache_key = f"players/exports/{component}/{export_id}.png"
    image_url = s3_client.get_public_url(cache_key)
    if image_url.startswith("/"):
        image_url = urljoin(str(request.base_url), image_url.lstrip("/"))

    cached = get_export_storage().check_cache(cache_key)

    safe_next = _sanitize_next(next)
    if not safe_next and cached and cached.redirect_path:
        safe_next = _sanitize_next(cached.redirect_path)

    next_url = (
        urljoin(str(request.base_url), safe_next.lstrip("/")) if safe_next else None
    )

    resolved_title = (title or "").strip() or None
    if not resolved_title:
        if cached and cached.title:
            resolved_title = cached.title

    resolved_title = resolved_title or "DraftGuru"
    resolved_next_url = next_url or urljoin(str(request.base_url), "")

    return request.app.state.templates.TemplateResponse(
        "share_card.html",
        {
            "request": request,
            "title": resolved_title,
            "image_url": image_url,
            "next_url": resolved_next_url,
        },
    )
