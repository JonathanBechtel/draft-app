"""Integration tests for T9: admin standalone name-comparison tool.

Covers:
- GET /admin/players/compare renders correctly (200, both forms present).
- Unauthenticated request redirects to login.
- "Find similar" mode with a mocked search returns candidate list with
  display_name, school, and score rendered.
- "Compare two names" mode with mocked embeds returns a cosine similarity
  value rendered on the page.

Gemini embed calls are patched — no real network calls.
DB interactions use the standard integration test stack (Postgres + pgvector).
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.auth_helpers import create_auth_user, login_staff

ADMIN_EMAIL = "compare-admin@example.com"
ADMIN_PASSWORD = "compare-admin-pw-123"

_DIMS = 768


def _unit_vec(dim: int) -> list[float]:
    """Return a 768-dim unit vector with 1.0 at position ``dim``."""
    v = [0.0] * _DIMS
    v[dim] = 1.0
    return v


@pytest_asyncio.fixture
async def admin_logged_in(app_client: AsyncClient, db_session: AsyncSession) -> None:
    """Create an admin user and establish a session cookie on ``app_client``."""
    await create_auth_user(
        db_session,
        email=ADMIN_EMAIL,
        role="admin",
        password=ADMIN_PASSWORD,
    )
    await login_staff(app_client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_page_redirects_unauthenticated(
    app_client: AsyncClient,
) -> None:
    """Unauthenticated GET request redirects to the login page.

    Expected: 303 redirect whose Location contains ``/admin/login``.
    The ``admin_logged_in`` fixture is intentionally NOT requested so the
    client has no session cookie.
    """
    app_client.cookies.clear()
    resp = await app_client.get(
        "/admin/players/compare",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/admin/login" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Page render (no query params)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_page_renders_empty(
    app_client: AsyncClient,
    admin_logged_in: None,
) -> None:
    """GET /admin/players/compare with no params returns 200 with both forms.

    Both the "Find similar" form (input#q) and the "Compare two names" form
    (input#name_a, input#name_b) must be present in the HTML.
    """
    _ = admin_logged_in
    resp = await app_client.get("/admin/players/compare")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="q"' in body
    assert 'id="name_a"' in body
    assert 'id="name_b"' in body


# ---------------------------------------------------------------------------
# "Find similar" mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_find_similar_renders_candidates(
    app_client: AsyncClient,
    admin_logged_in: None,
) -> None:
    """?q=mara triggers find_candidate_players and renders the candidate table.

    The mock returns a known Candidate set; we assert display_name, school,
    and score appear in the rendered HTML.

    Expected: 200, candidate rows for "Aday Mara" (0.8800) and "Cooper Flagg"
    (0.6000) in the table.
    """
    _ = admin_logged_in
    from app.services.player_search_service import Candidate

    fake_candidates = [
        Candidate(player_id=1, display_name="Aday Mara", school="Unicaja", score=0.88),
        Candidate(player_id=2, display_name="Cooper Flagg", school="Duke", score=0.60),
    ]

    with patch(
        "app.routes.admin.players.find_candidate_players",
        new=AsyncMock(return_value=fake_candidates),
    ):
        resp = await app_client.get("/admin/players/compare?q=mara")

    assert resp.status_code == 200
    body = resp.text
    assert "Aday Mara" in body
    assert "Unicaja" in body
    assert "0.8800" in body
    assert "Cooper Flagg" in body
    assert "0.6000" in body


@pytest.mark.asyncio
async def test_compare_find_similar_empty_results(
    app_client: AsyncClient,
    admin_logged_in: None,
) -> None:
    """?q=... with an empty result set renders the "no results" state.

    Expected: 200, "No results" text present.
    """
    _ = admin_logged_in
    with patch(
        "app.routes.admin.players.find_candidate_players",
        new=AsyncMock(return_value=[]),
    ):
        resp = await app_client.get("/admin/players/compare?q=nobody")

    assert resp.status_code == 200
    assert "No results" in resp.text


# ---------------------------------------------------------------------------
# "Compare two names" mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_two_names_renders_score(
    app_client: AsyncClient,
    admin_logged_in: None,
) -> None:
    """?name_a=Mara&name_b=Aday+Mara renders the cosine similarity value.

    Embeds are mocked to deterministic vectors:
    vec_a = e0, vec_b = e0 + e1  →  cosine similarity = 1/sqrt(2) ≈ 0.7071.
    Expected: 200, formatted score "0.7071" in the page.
    """
    _ = admin_logged_in
    vec_a = _unit_vec(0)
    vec_b = [0.0] * _DIMS
    vec_b[0] = 1.0
    vec_b[1] = 1.0

    expected_score = 1.0 / math.sqrt(2)

    embed_calls = [vec_a, vec_b]
    embed_iter = iter(embed_calls)

    async def _fake_embed(text: str) -> list[float]:
        return next(embed_iter)

    with patch(
        "app.services.player_search_service.embed_text",
        new=_fake_embed,
    ):
        resp = await app_client.get(
            "/admin/players/compare?name_a=Mara&name_b=Aday+Mara"
        )

    assert resp.status_code == 200
    body = resp.text
    formatted = f"{expected_score:.4f}"
    assert formatted in body, f"Expected score {formatted!r} not found in page body"
    assert "Mara" in body
    assert "Aday Mara" in body


@pytest.mark.asyncio
async def test_compare_two_names_high_similarity(
    app_client: AsyncClient,
    admin_logged_in: None,
) -> None:
    """Same vector for both inputs gives similarity == 1.0000.

    Expected: 200, "1.0000" in page, "High similarity" hint text.
    """
    _ = admin_logged_in
    vec = _unit_vec(3)

    async def _fake_embed(text: str) -> list[float]:
        return vec[:]

    with patch(
        "app.services.player_search_service.embed_text",
        new=_fake_embed,
    ):
        resp = await app_client.get(
            "/admin/players/compare?name_a=Cooper+Flagg&name_b=Cooper+Flagg"
        )

    assert resp.status_code == 200
    assert "1.0000" in resp.text
    assert "High similarity" in resp.text


@pytest.mark.asyncio
async def test_compare_two_names_low_similarity(
    app_client: AsyncClient,
    admin_logged_in: None,
) -> None:
    """Orthogonal vectors give similarity == 0.0000.

    Expected: 200, "0.0000" in page, "Low similarity" hint text.
    """
    _ = admin_logged_in
    vec_a = _unit_vec(0)
    vec_b = _unit_vec(1)

    embed_calls = [vec_a, vec_b]
    embed_iter = iter(embed_calls)

    async def _fake_embed(text: str) -> list[float]:
        return next(embed_iter)

    with patch(
        "app.services.player_search_service.embed_text",
        new=_fake_embed,
    ):
        resp = await app_client.get(
            "/admin/players/compare?name_a=Cooper+Flagg&name_b=Aday+Mara"
        )

    assert resp.status_code == 200
    assert "0.0000" in resp.text
    assert "Low similarity" in resp.text
