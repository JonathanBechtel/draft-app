"""Share-card image fetching observes caller-owned transaction boundaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from io import BytesIO

import pytest
from PIL import Image

from app.services.share_cards import image_embedder
from app.services.share_cards import model_builders


@pytest.mark.asyncio
async def test_image_fetch_releases_read_transaction_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary callback runs before the HTTP transport is reached."""
    released = False
    image_bytes = BytesIO()
    Image.new("RGB", (2, 2), color="red").save(image_bytes, format="PNG")

    async def _release() -> None:
        nonlocal released
        released = True

    class _Response:
        content = image_bytes.getvalue()

        def raise_for_status(self) -> None:
            return None

    class _Client:
        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, _url: str) -> _Response:
            assert released is True
            return _Response()

    monkeypatch.setattr(
        image_embedder.httpx,
        "AsyncClient",
        lambda **_kwargs: _Client(),
    )

    data_uri, has_photo = await image_embedder.fetch_and_embed_image(
        "https://example.test/player.png",
        "Player Name",
        before_fetch=_release,
    )

    assert has_photo is True
    assert data_uri is not None
    assert data_uri.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_player_badge_passes_session_boundary_to_image_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model builders release their read transaction before image I/O."""

    class _Db:
        commits = 0

        async def commit(self) -> None:
            self.commits += 1

    async def _player_info(
        _db: object, _player_id: int
    ) -> tuple[str, str, str, str, None, int]:
        return (
            "Player Name",
            "player-name",
            "G | School",
            "https://example.test/player.png",
            None,
            2026,
        )

    async def _fetch(
        _url: str,
        _name: str,
        *,
        before_fetch: Callable[[], Awaitable[None]],
    ) -> tuple[str, bool]:
        await before_fetch()
        return "data:image/png;base64,test", True

    monkeypatch.setattr(model_builders, "_resolve_player_info", _player_info)
    monkeypatch.setattr(model_builders, "fetch_and_embed_image", _fetch)
    db = _Db()

    badge = await model_builders._build_player_badge(db, 7)  # type: ignore[arg-type]

    assert db.commits == 1
    assert badge.has_photo is True
