"""Unit tests for the hybrid candidate blending logic in player_search_service.

Tests cover ``_merge_candidates`` (the pure blend/dedup function) and
``find_candidate_players`` (the async hybrid entry-point) with mocked lexical
and vector search functions.  No DB, no network, no Gemini calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.player_search_service import Candidate, _merge_candidates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _c(player_id: int, score: float, name: str = "Player") -> Candidate:
    """Build a Candidate with minimal fields for blending tests."""
    return Candidate(player_id=player_id, display_name=name, school=None, score=score)


# ---------------------------------------------------------------------------
# _merge_candidates — pure blend / dedup logic
# ---------------------------------------------------------------------------


class TestMergeCandidates:
    """Tests for the internal ``_merge_candidates`` function."""

    def test_empty_inputs_return_empty(self) -> None:
        """Both empty inputs produce an empty result."""
        assert _merge_candidates([], []) == []

    def test_lexical_only(self) -> None:
        """When vector list is empty, lexical results pass through unchanged."""
        lex = [_c(1, 0.9, "Cooper Flagg"), _c(2, 0.5, "Dylan Harper")]
        result = _merge_candidates(lex, [])
        assert len(result) == 2
        assert result[0].player_id == 1
        assert result[1].player_id == 2

    def test_vector_only(self) -> None:
        """When lexical list is empty, vector results pass through unchanged."""
        vec = [_c(10, 0.8, "Tre Johnson"), _c(11, 0.3, "Ron Holland")]
        result = _merge_candidates([], vec)
        assert len(result) == 2
        assert result[0].player_id == 10
        assert result[1].player_id == 11

    def test_dedup_by_player_id(self) -> None:
        """A player appearing in both lists appears exactly once in output."""
        lex = [_c(1, 0.7)]
        vec = [_c(1, 0.6)]
        result = _merge_candidates(lex, vec)
        assert len(result) == 1
        assert result[0].player_id == 1

    def test_strong_lexical_leads_over_higher_vector(self) -> None:
        """The key fix: a strong lexical match leads a higher-scored vector match.

        "mara" → Aday Mara (lexical 0.5) must beat vector noise (0.85), because
        cross-modality scores aren't comparable.
        """
        lex = [_c(1, 0.5, "Aday Mara")]
        vec = [_c(2, 0.85, "Jamar Butler")]
        result = _merge_candidates(lex, vec)
        assert [r.player_id for r in result] == [1, 2]

    def test_player_in_both_keeps_lexical_score(self) -> None:
        """A player in both lists appears once and keeps its lexical score (no max)."""
        lex = [_c(1, 0.4)]
        vec = [_c(1, 0.85)]
        result = _merge_candidates(lex, vec)
        assert len(result) == 1
        assert result[0].score == pytest.approx(0.4)

    def test_weak_lexical_ranks_below_vector(self) -> None:
        """Lexical matches below the priority threshold fall behind vector matches."""
        lex = [_c(1, 0.2, "coincidental trigram")]
        vec = [_c(2, 0.8, "semantic hit")]
        result = _merge_candidates(lex, vec)
        assert [r.player_id for r in result] == [2, 1]

    def test_order_is_strong_lexical_then_vector_then_weak(self) -> None:
        """Ordering: strong lexical, then vector, then weak lexical."""
        lex = [_c(1, 0.5), _c(2, 0.2)]  # id1 strong, id2 weak
        vec = [_c(3, 0.9)]
        result = _merge_candidates(lex, vec)
        assert [r.player_id for r in result] == [1, 3, 2]

    def test_union_of_ids(self) -> None:
        """All unique player_ids from both lists appear in the output."""
        lex = [_c(1, 0.6), _c(2, 0.5)]
        vec = [_c(2, 0.8), _c(3, 0.9)]
        result = _merge_candidates(lex, vec)
        ids = {r.player_id for r in result}
        assert ids == {1, 2, 3}

    def test_display_name_preserved_from_first_seen(self) -> None:
        """Metadata (display_name, school) is taken from the lexical result for ties."""
        lex = [Candidate(player_id=1, display_name="Aday Mara", school="Unicaja", score=0.7)]
        vec = [Candidate(player_id=1, display_name="Aday Mara", school="Unicaja", score=0.6)]
        result = _merge_candidates(lex, vec)
        assert result[0].display_name == "Aday Mara"
        assert result[0].school == "Unicaja"

    def test_no_mutation_of_inputs(self) -> None:
        """The function must not modify the input lists."""
        lex = [_c(1, 0.5)]
        vec = [_c(2, 0.9)]
        lex_orig = list(lex)
        vec_orig = list(vec)
        _merge_candidates(lex, vec)
        assert lex == lex_orig
        assert vec == vec_orig


# ---------------------------------------------------------------------------
# find_candidate_players — hybrid async entry-point
# ---------------------------------------------------------------------------


class TestFindCandidatePlayers:
    """Tests for the async ``find_candidate_players`` function."""

    @pytest.mark.asyncio
    async def test_returns_merged_results(self) -> None:
        """Hybrid function merges lexical + vector results into a single ranked list."""
        from app.services.player_search_service import find_candidate_players

        lex_result = [_c(1, 0.8, "Aday Mara")]
        vec_result = [_c(2, 0.9, "Cooper Flagg")]

        db = AsyncMock()
        with (
            patch(
                "app.services.player_search_service.find_lexical_players",
                new=AsyncMock(return_value=lex_result),
            ),
            patch(
                "app.services.player_search_service.find_similar_players",
                new=AsyncMock(return_value=vec_result),
            ),
        ):
            result = await find_candidate_players(db, "mara", k=5)

        ids = {r.player_id for r in result}
        assert ids == {1, 2}
        # Lexical priority: the strong lexical hit (Aday Mara) leads even though
        # the vector hit (Cooper Flagg) has a higher raw score.
        assert result[0].player_id == 1
        assert result[1].player_id == 2

    @pytest.mark.asyncio
    async def test_dedup_in_async_path(self) -> None:
        """Player appearing in both lexical and vector results deduped to one entry."""
        from app.services.player_search_service import find_candidate_players

        shared = _c(42, 0.7, "Aday Mara")
        lex_result = [shared]
        vec_result = [_c(42, 0.5, "Aday Mara")]

        db = AsyncMock()
        with (
            patch(
                "app.services.player_search_service.find_lexical_players",
                new=AsyncMock(return_value=lex_result),
            ),
            patch(
                "app.services.player_search_service.find_similar_players",
                new=AsyncMock(return_value=vec_result),
            ),
        ):
            result = await find_candidate_players(db, "mara", k=5)

        assert len(result) == 1
        assert result[0].player_id == 42
        assert result[0].score == pytest.approx(0.7)  # lexical entry leads, keeps its score

    @pytest.mark.asyncio
    async def test_k_truncates_output(self) -> None:
        """Result is truncated to k candidates after merging."""
        from app.services.player_search_service import find_candidate_players

        lex_result = [_c(i, 0.9 - i * 0.1) for i in range(4)]
        vec_result: list[Candidate] = []

        db = AsyncMock()
        with (
            patch(
                "app.services.player_search_service.find_lexical_players",
                new=AsyncMock(return_value=lex_result),
            ),
            patch(
                "app.services.player_search_service.find_similar_players",
                new=AsyncMock(return_value=vec_result),
            ),
        ):
            result = await find_candidate_players(db, "player", k=2)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_empty_both_sources_returns_empty(self) -> None:
        """Returns empty list when both lexical and vector searches find nothing."""
        from app.services.player_search_service import find_candidate_players

        db = AsyncMock()
        with (
            patch(
                "app.services.player_search_service.find_lexical_players",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.player_search_service.find_similar_players",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await find_candidate_players(db, "nobody", k=5)

        assert result == []

    @pytest.mark.asyncio
    async def test_lexical_only_player_still_surfaces(self) -> None:
        """A player with no embedding (lexical-only hit) still appears in output."""
        from app.services.player_search_service import find_candidate_players

        lex_result = [_c(99, 0.6, "Some Guy")]
        vec_result: list[Candidate] = []  # no embedding

        db = AsyncMock()
        with (
            patch(
                "app.services.player_search_service.find_lexical_players",
                new=AsyncMock(return_value=lex_result),
            ),
            patch(
                "app.services.player_search_service.find_similar_players",
                new=AsyncMock(return_value=vec_result),
            ),
        ):
            result = await find_candidate_players(db, "some guy", k=5)

        assert len(result) == 1
        assert result[0].player_id == 99
        assert result[0].score == pytest.approx(0.6)

    @pytest.mark.asyncio
    async def test_vector_only_player_still_surfaces(self) -> None:
        """A player only found by vector (no trigram hit) still appears."""
        from app.services.player_search_service import find_candidate_players

        lex_result: list[Candidate] = []
        vec_result = [_c(77, 0.85, "Cooper Flagg")]

        db = AsyncMock()
        with (
            patch(
                "app.services.player_search_service.find_lexical_players",
                new=AsyncMock(return_value=lex_result),
            ),
            patch(
                "app.services.player_search_service.find_similar_players",
                new=AsyncMock(return_value=vec_result),
            ),
        ):
            result = await find_candidate_players(db, "flagg", k=5)

        assert len(result) == 1
        assert result[0].player_id == 77
