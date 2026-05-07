"""SEO routes: ``robots.txt`` and ``sitemap.xml``."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.schemas.players_master import PlayerMaster
from app.utils.db_async import get_session

router = APIRouter()


# (path, changefreq, priority) for the public HTML pages worth surfacing.
_STATIC_PAGES: tuple[tuple[str, str, str], ...] = (
    ("/", "daily", "1.0"),
    ("/news", "hourly", "0.9"),
    ("/podcasts", "daily", "0.8"),
    ("/film-room", "daily", "0.7"),
    ("/terms", "yearly", "0.1"),
    ("/privacy", "yearly", "0.1"),
    ("/cookies", "yearly", "0.1"),
)


def _site_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> Response:
    base = _site_base(request)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    stmt = (
        select(PlayerMaster.slug, PlayerMaster.updated_at)  # type: ignore[call-overload]
        .where(PlayerMaster.is_stub == False)  # noqa: E712
        .where(PlayerMaster.slug.isnot(None))  # type: ignore[union-attr]
        .where(PlayerMaster.display_name.isnot(None))  # type: ignore[union-attr]
        .order_by(PlayerMaster.updated_at.desc())  # type: ignore[attr-defined]
    )
    result = await db.execute(stmt)

    parts: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>']
    parts.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')

    for path, changefreq, priority in _STATIC_PAGES:
        parts.append(
            f"<url><loc>{base}{path}</loc>"
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>{changefreq}</changefreq>"
            f"<priority>{priority}</priority></url>"
        )

    for slug, updated_at in result.all():
        lastmod = updated_at.strftime("%Y-%m-%d") if updated_at else today
        parts.append(
            f"<url><loc>{base}/players/{slug}</loc>"
            f"<lastmod>{lastmod}</lastmod>"
            f"<changefreq>weekly</changefreq>"
            f"<priority>0.6</priority></url>"
        )

    parts.append("</urlset>")
    return Response(content="".join(parts), media_type="application/xml")


@router.get("/robots.txt", include_in_schema=False, response_class=PlainTextResponse)
async def robots_txt(request: Request) -> str:
    base = _site_base(request)
    return f"User-agent: *\nDisallow: /admin\n\nSitemap: {base}/sitemap.xml\n"
