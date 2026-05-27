"""Integration tests for the hybrid (pg_trgm + vector) candidate search.

Validates that ``find_candidate_players`` surfaces the correct in-DB player
for a bare surname query (e.g. ``"mara"`` → Aday Mara) where pure vector
search would miss — the vector for a short token matches on surface form
rather than on the player's actual embedding vector.

No live Gemini calls occur:
  - ``embed_text`` is patched to return a deterministic vector that is
    deliberately far from Aday Mara's stored vector so vector-only search
    would rank Mara low or miss him entirely.
  - The player_embeddings row for Aday Mara uses a unit vector along dim 1.
  - The "mara" query is given a unit vector along dim 0 (orthogonal →
    cosine similarity ≈ 0.0), so vector alone would not surface Mara.
  - The trigram similarity of ``"mara"`` to ``"Aday Mara"`` is ~0.44, well
    above the 0.1 threshold, so the lexical path surfaces him.

Seeded data:
  - Cooper Flagg (Duke):   embedding = e0 (1, 0, 0, ...)
  - Dylan Harper (Rutgers): embedding = e1 (0, 1, 0, ...)
  - Aday Mara (Unicaja):    embedding = e1 (0, 1, 0, ...)
  - "Mara" alias for Aday Mara: validates alias trigram search path

Query vectors:
  - "mara" bare-surname query → patched to e0 (far from Mara's e1 vector)
  - Full-name queries use the player's actual vector direction.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def hybrid_seeded_db(db_session: AsyncSession) -> dict[str, int]:
    """Seed three players + embeddings for hybrid search tests.

    Vectors are chosen so that a query vector of e0 (dim 0) is:
    - Similar to Cooper Flagg (e0, sim ≈ 1.0)
    - Orthogonal to Aday Mara (e1, sim ≈ 0.0)  ← vector alone would miss him
    - Orthogonal to Dylan Harper (e1, sim ≈ 0.0)

    The trigram path must rescue Aday Mara when the query is "mara".

    Returns:
        A mapping of ``"First Last"`` → ``player_id``.
    """
    from app.schemas.player_aliases import PlayerAlias
    from app.schemas.player_embeddings import PlayerEmbedding

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    players_data = [
        ("Cooper", "Flagg", "Duke", _unit_vec(0)),
        ("Dylan", "Harper", "Rutgers", _unit_vec(1)),
        ("Aday", "Mara", "Unicaja", _unit_vec(1)),  # embedding far from e0
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

    # Add a "Mara" alias for Aday Mara so we can confirm alias trigram path.
    alias = PlayerAlias(
        player_id=id_map["Aday Mara"],
        full_name="Mara",
        context="test-bare-surname-alias",
        created_at=now,
    )
    db_session.add(alias)
    await db_session.flush()
    await db_session.commit()
    return id_map


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_surfaces_bare_surname(
    db_session: AsyncSession,
    hybrid_seeded_db: dict[str, int],
) -> None:
    """Bare surname "mara" surfaces Aday Mara via the trigram path.

    The query vector is e0 (orthogonal to Mara's stored e1 vector), so pure
    cosine search would give Mara a score of ~0.0.  Trigram similarity of
    "mara" to "Aday Mara" is ~0.44, so the hybrid result must include Mara.
    """
    from app.services.player_search_service import find_candidate_players

    # Patch embed_text to return e0 — deliberately far from Mara's e1 vector.
    query_vec = _unit_vec(0)

    with patch(
        "app.services.player_search_service.embed_text",
        new=AsyncMock(return_value=query_vec),
    ):
        results = await find_candidate_players(db_session, "mara", k=5)

    player_ids = {r.player_id for r in results}
    assert hybrid_seeded_db["Aday Mara"] in player_ids, (
        f"Expected Aday Mara (id={hybrid_seeded_db['Aday Mara']}) in hybrid results, "
        f"got ids={player_ids}"
    )


@pytest.mark.asyncio
async def test_hybrid_bare_surname_mara_ranks_above_threshold(
    db_session: AsyncSession,
    hybrid_seeded_db: dict[str, int],
) -> None:
    """Aday Mara's combined score must be above the trigram threshold (0.1).

    With e0 query vector, vector score for Mara ≈ 0.0.  But trigram score
    of "mara" vs "Aday Mara" is high enough to clear the threshold.
    """
    from app.services.player_search_service import find_candidate_players

    query_vec = _unit_vec(0)

    with patch(
        "app.services.player_search_service.embed_text",
        new=AsyncMock(return_value=query_vec),
    ):
        results = await find_candidate_players(db_session, "mara", k=5)

    mara_candidates = [r for r in results if r.player_id == hybrid_seeded_db["Aday Mara"]]
    assert mara_candidates, "Aday Mara not found in hybrid results"
    mara = mara_candidates[0]
    assert mara.score > 0.1, f"Expected score > 0.1, got {mara.score}"


@pytest.mark.asyncio
async def test_hybrid_full_name_still_works(
    db_session: AsyncSession,
    hybrid_seeded_db: dict[str, int],
) -> None:
    """A full-name query "Cooper Flagg" still surfaces Flagg as the top result.

    Query vector is e0 — matches Flagg's stored e0 vector with cosine sim = 1.0.
    Both lexical and vector paths should agree on Flagg.
    """
    from app.services.player_search_service import find_candidate_players

    query_vec = _unit_vec(0)  # Flagg's vector

    with patch(
        "app.services.player_search_service.embed_text",
        new=AsyncMock(return_value=query_vec),
    ):
        results = await find_candidate_players(db_session, "Cooper Flagg", k=5)

    assert results, "Hybrid search returned no results for 'Cooper Flagg'"
    assert results[0].player_id == hybrid_seeded_db["Cooper Flagg"], (
        f"Expected Cooper Flagg (id={hybrid_seeded_db['Cooper Flagg']}) first, "
        f"got id={results[0].player_id} ({results[0].display_name})"
    )
    assert results[0].score == pytest.approx(1.0, abs=1e-5)


@pytest.mark.asyncio
async def test_hybrid_deduplicates_when_player_in_both(
    db_session: AsyncSession,
    hybrid_seeded_db: dict[str, int],
) -> None:
    """When a player appears in both lexical and vector results, they appear once."""
    from app.services.player_search_service import find_candidate_players

    # "Cooper Flagg" will match both trigram and vector.
    query_vec = _unit_vec(0)

    with patch(
        "app.services.player_search_service.embed_text",
        new=AsyncMock(return_value=query_vec),
    ):
        results = await find_candidate_players(db_session, "Cooper Flagg", k=5)

    flagg_id = hybrid_seeded_db["Cooper Flagg"]
    flagg_hits = [r for r in results if r.player_id == flagg_id]
    assert len(flagg_hits) == 1, f"Expected 1 Flagg result, got {len(flagg_hits)}"


@pytest.mark.asyncio
async def test_vector_alone_misses_mara(
    db_session: AsyncSession,
    hybrid_seeded_db: dict[str, int],
) -> None:
    """Baseline: pure vector search misses Aday Mara when query vector is e0.

    This confirms the motivation for the hybrid approach — vector alone is
    insufficient for bare/short-surname queries.

    With e0 as the query and Mara stored on e1, cosine distance is 1.0 →
    score ≈ 0.0.  Mara may still appear in the top-5 (all scores are low)
    but ranked last or with a score near zero.
    """
    from app.services.player_search_service import find_similar_players

    query_vec = _unit_vec(0)

    with patch(
        "app.services.player_search_service.embed_text",
        new=AsyncMock(return_value=query_vec),
    ):
        results = await find_similar_players(db_session, "mara", k=5)

    # All three players are in the DB.  Mara's cosine similarity ≈ 0.0
    # (orthogonal vectors), so if Mara appears, it should be ranked last
    # with a score near zero — demonstrating pure vector is insufficient.
    mara_candidates = [r for r in results if r.player_id == hybrid_seeded_db["Aday Mara"]]
    if mara_candidates:
        mara = mara_candidates[0]
        # Confirm Mara's vector score is near-zero (orthogonal vectors)
        assert mara.score == pytest.approx(0.0, abs=1e-4), (
            f"Vector-only score for Mara should be ~0 with e0 query, got {mara.score}"
        )


@pytest.mark.asyncio
async def test_hybrid_empty_table_returns_empty(
    db_session: AsyncSession,
) -> None:
    """When no players exist, hybrid search returns an empty list without error."""
    from app.services.player_search_service import find_candidate_players

    query_vec = _unit_vec(0)

    with patch(
        "app.services.player_search_service.embed_text",
        new=AsyncMock(return_value=query_vec),
    ):
        results = await find_candidate_players(db_session, "nobody", k=5)

    assert results == []
