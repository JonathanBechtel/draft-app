"""Unit tests for ``app.services.player_search_service``.

No DB, no network.  ``embed_text`` is patched so no Gemini calls occur.
The asyncpg + SQLAlchemy connection stack is fully mocked so tests run
offline and are deterministic.

The service uses the raw asyncpg connection (``asyncpg_conn.fetch()``) to
avoid SQLAlchemy's PREPARE step, which cannot resolve the ``vector`` type
when ``search_path`` excludes ``public``.  The mock therefore mimics the
chain: ``db.connection()`` → ``get_raw_connection()`` → ``driver_connection``
→ ``fetchrow()`` / ``fetch()``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.player_search_service import Candidate, find_similar_players


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_VECTOR: list[float] = [0.1] * 768


def _make_asyncpg_record(
    player_id: int,
    display_name: str | None,
    school: str | None,
    score: float,
) -> dict[str, Any]:
    """Return a dict that mimics an asyncpg ``Record`` (subscript access)."""
    return {
        "player_id": player_id,
        "display_name": display_name,
        "school": school,
        "score": score,
    }


def _make_db_session(records: list[dict[str, Any]], current_path: str = "public") -> Any:
    """Build a fully-mocked async DB session that returns ``records`` on fetch.

    Mimics the chain:
    ``db.connection()`` →
    ``async_conn.get_raw_connection()`` →
    ``raw_conn.driver_connection`` (asyncpg conn) →
    ``asyncpg_conn.fetchrow()`` / ``asyncpg_conn.fetch()`` / ``execute()``
    """
    path_record: dict[str, str] = {"sp": current_path}

    asyncpg_conn = MagicMock()
    asyncpg_conn.fetchrow = AsyncMock(return_value=path_record)
    asyncpg_conn.execute = AsyncMock(return_value=None)
    asyncpg_conn.fetch = AsyncMock(return_value=records)

    raw_conn = MagicMock()
    raw_conn.driver_connection = asyncpg_conn

    async_conn = AsyncMock()
    async_conn.get_raw_connection = AsyncMock(return_value=raw_conn)

    db = AsyncMock()
    db.connection = AsyncMock(return_value=async_conn)
    return db


# ---------------------------------------------------------------------------
# find_similar_players — basic contract
# ---------------------------------------------------------------------------


class TestFindSimilarPlayers:
    """Tests for the k-NN search service function."""

    @pytest.mark.asyncio
    async def test_returns_candidates_in_score_order(self) -> None:
        """Results must be in the order returned by the SQL query (desc score).

        The SQL uses ORDER BY distance ASC → highest similarity first.
        The service preserves that ordering exactly.
        """
        records = [
            _make_asyncpg_record(1, "Cooper Flagg", "Duke", 0.95),
            _make_asyncpg_record(2, "Dylan Harper", "Rutgers", 0.80),
            _make_asyncpg_record(3, "Tre Johnson", "Texas", 0.65),
        ]
        db = _make_db_session(records)

        with patch(
            "app.services.player_search_service.embed_text",
            new=AsyncMock(return_value=FAKE_VECTOR),
        ):
            results = await find_similar_players(db, "flagg duke usa", k=3)

        assert len(results) == 3
        assert results[0].player_id == 1
        assert results[1].player_id == 2
        assert results[2].player_id == 3
        assert results[0].score > results[1].score > results[2].score

    @pytest.mark.asyncio
    async def test_candidate_fields_populated_correctly(self) -> None:
        """Each Candidate carries player_id, display_name, school, and score."""
        records = [_make_asyncpg_record(42, "Aday Mara", "Unicaja", 0.88)]
        db = _make_db_session(records)

        with patch(
            "app.services.player_search_service.embed_text",
            new=AsyncMock(return_value=FAKE_VECTOR),
        ):
            results = await find_similar_players(db, "mara", k=1)

        assert len(results) == 1
        c = results[0]
        assert isinstance(c, Candidate)
        assert c.player_id == 42
        assert c.display_name == "Aday Mara"
        assert c.school == "Unicaja"
        assert c.score == pytest.approx(0.88)

    @pytest.mark.asyncio
    async def test_none_display_name_and_school_handled(self) -> None:
        """Candidate fields may be None for stub players with sparse data."""
        records = [_make_asyncpg_record(7, None, None, 0.70)]
        db = _make_db_session(records)

        with patch(
            "app.services.player_search_service.embed_text",
            new=AsyncMock(return_value=FAKE_VECTOR),
        ):
            results = await find_similar_players(db, "unknown player", k=1)

        assert results[0].display_name is None
        assert results[0].school is None

    @pytest.mark.asyncio
    async def test_empty_result_set(self) -> None:
        """When the table is empty, an empty list is returned without error."""
        db = _make_db_session([])

        with patch(
            "app.services.player_search_service.embed_text",
            new=AsyncMock(return_value=FAKE_VECTOR),
        ):
            results = await find_similar_players(db, "nobody", k=5)

        assert results == []

    @pytest.mark.asyncio
    async def test_k_limits_candidate_count(self) -> None:
        """At most k rows are requested; the mock returns exactly k records."""
        records = [
            _make_asyncpg_record(i, f"Player {i}", "School", 0.9 - i * 0.1)
            for i in range(3)
        ]
        db = _make_db_session(records)

        with patch(
            "app.services.player_search_service.embed_text",
            new=AsyncMock(return_value=FAKE_VECTOR),
        ):
            results = await find_similar_players(db, "player", k=3)

        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_embed_text_called_with_query(self) -> None:
        """``embed_text`` must be called with the exact query string passed in."""
        db = _make_db_session([])

        embed_mock = AsyncMock(return_value=FAKE_VECTOR)
        with patch("app.services.player_search_service.embed_text", new=embed_mock):
            await find_similar_players(db, "Cooper Flagg Duke USA", k=5)

        embed_mock.assert_called_once_with("Cooper Flagg Duke USA")

    @pytest.mark.asyncio
    async def test_score_clamped_to_unit_interval(self) -> None:
        """Floating-point noise that pushes score outside [0,1] is clamped."""
        records = [
            _make_asyncpg_record(1, "P1", "S1", 1.0000001),   # clamp to 1.0
            _make_asyncpg_record(2, "P2", "S2", -0.0000001),  # clamp to 0.0
        ]
        db = _make_db_session(records)

        with patch(
            "app.services.player_search_service.embed_text",
            new=AsyncMock(return_value=FAKE_VECTOR),
        ):
            results = await find_similar_players(db, "test", k=2)

        assert results[0].score == pytest.approx(1.0)
        assert results[1].score == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_embed_text_error_propagates(self) -> None:
        """If ``embed_text`` raises, the exception bubbles up to the caller."""
        db = _make_db_session([])

        with patch(
            "app.services.player_search_service.embed_text",
            new=AsyncMock(
                side_effect=ValueError("embed_text received an empty string.")
            ),
        ):
            with pytest.raises(ValueError, match="empty string"):
                await find_similar_players(db, "   ", k=5)

    @pytest.mark.asyncio
    async def test_search_path_widened_then_restored_when_public_absent(self) -> None:
        """When ``public`` is absent, search_path is widened then restored in finally.

        The connection is pooled, so the widened path must not leak to whoever
        reuses it next: we expect two SET statements — widen, then restore the
        original narrow path.
        """
        records: list[dict[str, Any]] = []
        db = _make_db_session(records, current_path='"pytest_schema_abc123"')

        async_conn = await db.connection()
        raw_conn = await async_conn.get_raw_connection()
        asyncpg_conn = raw_conn.driver_connection

        with patch(
            "app.services.player_search_service.embed_text",
            new=AsyncMock(return_value=FAKE_VECTOR),
        ):
            await find_similar_players(db, "test", k=5)

        assert asyncpg_conn.execute.call_count == 2
        widen_sql = asyncpg_conn.execute.call_args_list[0][0][0]
        restore_sql = asyncpg_conn.execute.call_args_list[1][0][0]
        assert "search_path" in widen_sql and "public" in widen_sql
        assert "search_path" in restore_sql
        # Restored to the original narrow path — public must not linger.
        assert "public" not in restore_sql
        assert '"pytest_schema_abc123"' in restore_sql

    @pytest.mark.asyncio
    async def test_search_path_not_modified_when_public_present(self) -> None:
        """When ``public`` is already in search_path, SET LOCAL is NOT called."""
        records: list[dict[str, Any]] = []
        db = _make_db_session(records, current_path="public")

        async_conn = await db.connection()
        raw_conn = await async_conn.get_raw_connection()
        asyncpg_conn = raw_conn.driver_connection

        with patch(
            "app.services.player_search_service.embed_text",
            new=AsyncMock(return_value=FAKE_VECTOR),
        ):
            await find_similar_players(db, "test", k=5)

        asyncpg_conn.execute.assert_not_called()
