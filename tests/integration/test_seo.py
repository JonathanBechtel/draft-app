"""Integration tests for sitemap.xml and robots.txt."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster


@pytest.mark.asyncio
async def test_robots_txt_serves_directives_and_sitemap_link(
    app_client: AsyncClient,
) -> None:
    """robots.txt returns plain text with disallow rules and a sitemap pointer."""
    response = await app_client.get("/robots.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "User-agent: *" in body
    assert "Disallow: /admin" in body
    assert "Sitemap:" in body
    assert "/sitemap.xml" in body


@pytest.mark.asyncio
async def test_sitemap_includes_static_pages_and_real_players(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """sitemap.xml lists each static page and each non-stub player slug."""
    real_player = PlayerMaster(
        display_name="Sitemap Real Player",
        first_name="Sitemap",
        last_name="Real",
        is_stub=False,
    )
    stub_player = PlayerMaster(
        display_name="Sitemap Stub Player",
        first_name="Sitemap",
        last_name="Stub",
        is_stub=True,
    )
    db_session.add_all([real_player, stub_player])
    await db_session.commit()
    await db_session.refresh(real_player)
    await db_session.refresh(stub_player)

    response = await app_client.get("/sitemap.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    body = response.text
    assert body.startswith('<?xml version="1.0"')
    assert "<urlset" in body
    # Static pages are included
    for path in ("/news", "/podcasts", "/film-room", "/terms"):
        assert f"{path}</loc>" in body, f"missing static path {path}"
    # Real player appears, stub does not
    assert f"/players/{real_player.slug}" in body
    assert f"/players/{stub_player.slug}" not in body
