"""Integration tests for ``app.services.player_search_service``.

These tests validate the SQL k-NN plumbing and cosine-similarity score math
end-to-end against a real Postgres + pgvector instance.  No Gemini calls are
made: ``embed_text`` is patched to return hand-crafted deterministic vectors,
and the corresponding ``player_embeddings`` rows are inserted directly with
those same vectors so we can predict the expected similarity ordering.

Vector geometry used in this test:
    - v_a = [1, 0, 0, ...]   (unit vector along dim 0)
    - v_b = [0, 1, 0, ...]   (unit vector along dim 1)
    - v_c = [1, 1, 0, ...]   (45-degree between a and b; NOT normalised)

    cosine_similarity(v_a, v_a) = 1.0
    cosine_similarity(v_b, v_b) = 1.0
    cosine_similarity(v_a, v_b) = 0.0
    cosine_similarity(v_a, v_c) = 1/sqrt(2) ≈ 0.7071

For a query of v_a, the expected ranking is: player_a (score≈1.0) > player_c
(score≈0.707) > player_b (score≈0.0).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import make_player


# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------

_DIMS = 768


def _unit_vec(dim: int) -> list[float]:
    """Return a 768-dim unit vector with a 1.0 at position ``dim``."""
    v = [0.0] * _DIMS
    v[dim] = 1.0
    return v


def _diag_vec(*dims: int) -> list[float]:
    """Return a 768-dim vector with 1.0 at each of the given dim positions."""
    v = [0.0] * _DIMS
    for d in dims:
        v[d] = 1.0
    return v


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def seeded_db(db_session: AsyncSession) -> dict[str, int]:
    """Seed three players + embeddings with deterministic vectors.

    Returns a mapping ``{player_name: player_id}`` so tests can assert on IDs.
    """
    from app.schemas.player_embeddings import PlayerEmbedding

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    players_data = [
        ("Cooper", "Flagg", "Duke", _unit_vec(0)),        # e0 axis
        ("Dylan", "Harper", "Rutgers", _unit_vec(1)),      # e1 axis
        ("Aday", "Mara", "Unicaja", _diag_vec(0, 1)),      # 45° between e0 and e1
    ]

    id_map: dict[str, int] = {}
    for first, last, school, vec in players_data:
        player = make_player(first, last, school=school)
        db_session.add(player)
        await db_session.flush()
        assert player.id is not None

        row = PlayerEmbedding(
            player_id=player.id,
            embedding=vec,
            model_name="text-embedding-004-test",
            created_at=now,
            updated_at=now,
        )
        db_session.add(row)
        await db_session.flush()

        id_map[f"{first} {last}"] = player.id

    await db_session.commit()
    return id_map


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_similar_players_ordering(
    db_session: AsyncSession,
    seeded_db: dict[str, int],
) -> None:
    """Results are returned in descending cosine-similarity order.

    Query vector = e0 (unit vector along dim 0).  Expected ranking:
    1. Cooper Flagg  (stored vec = e0;     sim ≈ 1.000)
    2. Aday Mara     (stored vec = e0+e1;  sim ≈ 0.707)
    3. Dylan Harper  (stored vec = e1;     sim ≈ 0.000)
    """
    from app.services.player_search_service import find_similar_players

    query_vec = _unit_vec(0)

    with patch(
        "app.services.player_search_service.embed_text",
        new=AsyncMock(return_value=query_vec),
    ):
        results = await find_similar_players(db_session, "Cooper Flagg Duke", k=3)

    assert len(results) == 3

    names = [r.display_name for r in results]
    assert names[0] == "Cooper Flagg", f"Expected Cooper Flagg first, got {names}"
    assert names[1] == "Aday Mara", f"Expected Aday Mara second, got {names}"
    assert names[2] == "Dylan Harper", f"Expected Dylan Harper third, got {names}"


@pytest.mark.asyncio
async def test_find_similar_players_scores(
    db_session: AsyncSession,
    seeded_db: dict[str, int],
) -> None:
    """Score values are mathematically correct (1 - cosine_distance).

    Query = e0:
    - Flagg  (e0):      score == 1.0
    - Mara   (e0+e1):   score == 1/sqrt(2) ≈ 0.7071
    - Harper (e1):      score ≈ 0.0
    """
    from app.services.player_search_service import find_similar_players

    query_vec = _unit_vec(0)

    with patch(
        "app.services.player_search_service.embed_text",
        new=AsyncMock(return_value=query_vec),
    ):
        results = await find_similar_players(db_session, "Cooper Flagg", k=3)

    by_name = {r.display_name: r.score for r in results}

    assert by_name["Cooper Flagg"] == pytest.approx(1.0, abs=1e-6)
    assert by_name["Aday Mara"] == pytest.approx(1.0 / math.sqrt(2), abs=1e-6)
    assert by_name["Dylan Harper"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_find_similar_players_respects_k(
    db_session: AsyncSession,
    seeded_db: dict[str, int],
) -> None:
    """Passing k=1 returns exactly one candidate (the closest match)."""
    from app.services.player_search_service import find_similar_players

    query_vec = _unit_vec(1)  # closest to Harper

    with patch(
        "app.services.player_search_service.embed_text",
        new=AsyncMock(return_value=query_vec),
    ):
        results = await find_similar_players(db_session, "Dylan Harper Rutgers", k=1)

    assert len(results) == 1
    assert results[0].display_name == "Dylan Harper"
    assert results[0].score == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_find_similar_players_candidate_fields(
    db_session: AsyncSession,
    seeded_db: dict[str, int],
) -> None:
    """Returned Candidates carry correct player_id, display_name, and school."""
    from app.services.player_search_service import Candidate, find_similar_players

    query_vec = _unit_vec(0)

    with patch(
        "app.services.player_search_service.embed_text",
        new=AsyncMock(return_value=query_vec),
    ):
        results = await find_similar_players(db_session, "flagg", k=1)

    assert len(results) == 1
    top = results[0]
    assert isinstance(top, Candidate)
    assert top.player_id == seeded_db["Cooper Flagg"]
    assert top.display_name == "Cooper Flagg"
    assert top.school == "Duke"
    assert 0.0 <= top.score <= 1.0


@pytest.mark.asyncio
async def test_find_similar_players_empty_table(
    db_session: AsyncSession,
) -> None:
    """When no embeddings exist, an empty list is returned without error."""
    from app.services.player_search_service import find_similar_players

    query_vec = _unit_vec(0)

    with patch(
        "app.services.player_search_service.embed_text",
        new=AsyncMock(return_value=query_vec),
    ):
        results = await find_similar_players(db_session, "nobody", k=5)

    assert results == []
