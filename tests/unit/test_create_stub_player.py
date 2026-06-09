"""Unit tests for the public create_stub_player wrapper.

These tests exercise the four outcome branches (created, blocked_existing,
ambiguous, rejected_guard) without hitting the database, by patching the
internal helpers used by the wrapper.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.player_mention_service import (
    PlayerMatch,
    StubCreateResult,
    _LookupEntry,
    _PlayerNameLookup,
    create_stub_player,
)


def _empty_lookup() -> _PlayerNameLookup:
    """Return a lookup with no entries (simulates no existing players)."""
    return _PlayerNameLookup(
        display_exact={},
        alias_exact={},
        display_relaxed={},
        alias_relaxed={},
    )


def _lookup_with_one_player(
    player_id: int, display_name: str
) -> _PlayerNameLookup:
    """Return a lookup populated so the given player matches its own display name."""
    from app.services.player_mention_service import (
        _normalized_name_key,
    )

    entry = _LookupEntry(
        player_id=player_id,
        display_name=display_name,
        matched_via="display_name",
    )
    exact_key = _normalized_name_key(display_name)
    relaxed_key = _normalized_name_key(
        display_name, ignore_suffix=True, ignore_middle_initials=True
    )
    return _PlayerNameLookup(
        display_exact={exact_key: {player_id: entry}},
        alias_exact={},
        display_relaxed={relaxed_key: {player_id: entry}},
        alias_relaxed={},
    )


def _lookup_with_two_players_same_key(
    player_id_a: int,
    display_a: str,
    player_id_b: int,
    display_b: str,
    shared_relaxed_key: str,
) -> _PlayerNameLookup:
    """Return a lookup where two distinct players share a relaxed key (ambiguous)."""
    entry_a = _LookupEntry(
        player_id=player_id_a,
        display_name=display_a,
        matched_via="display_name",
    )
    entry_b = _LookupEntry(
        player_id=player_id_b,
        display_name=display_b,
        matched_via="display_name",
    )
    return _PlayerNameLookup(
        display_exact={},
        alias_exact={},
        display_relaxed={shared_relaxed_key: {player_id_a: entry_a, player_id_b: entry_b}},
        alias_relaxed={},
    )


class TestCreateStubPlayerRejectedGuard:
    """Tests for the rejected_guard outcome branch."""

    @pytest.mark.asyncio
    async def test_empty_name_is_rejected(self) -> None:
        """An empty string must be rejected before any DB call."""
        db = MagicMock()
        result = await create_stub_player(db, "")
        assert result.outcome == "rejected_guard"
        assert result.player_id is None
        assert result.reason is not None

    @pytest.mark.asyncio
    async def test_whitespace_only_name_is_rejected(self) -> None:
        """Whitespace-only input must be rejected before any DB call."""
        db = MagicMock()
        result = await create_stub_player(db, "   ")
        assert result.outcome == "rejected_guard"
        assert result.reason is not None

    @pytest.mark.asyncio
    async def test_single_token_name_is_rejected(self) -> None:
        """Single-token names (no last name) must be rejected — too vague."""
        db = MagicMock()
        result = await create_stub_player(db, "Flagg")
        assert result.outcome == "rejected_guard"
        assert result.reason is not None
        assert result.player_id is None

    @pytest.mark.asyncio
    async def test_single_token_with_suffix_is_rejected(self) -> None:
        """A name token plus a recognized suffix only yields one substance token."""
        db = MagicMock()
        result = await create_stub_player(db, "Cooper Jr.")
        # After suffix stripping only "Cooper" remains — single token.
        assert result.outcome == "rejected_guard"


class TestCreateStubPlayerBlockedExisting:
    """Tests for the blocked_existing outcome branch."""

    @pytest.mark.asyncio
    async def test_unique_match_blocks_creation(self) -> None:
        """A unique existing match must block creation and return that player."""
        db = MagicMock()
        existing = PlayerMatch(
            player_id=99, display_name="Cooper Flagg", matched_via="display_name"
        )
        lookup = _lookup_with_one_player(99, "Cooper Flagg")

        with (
            patch(
                "app.services.player_mention_service._build_player_name_lookup",
                new=AsyncMock(return_value=lookup),
            ),
            patch(
                "app.services.player_mention_service._create_stub_player",
                new=AsyncMock(),
            ) as mock_create,
        ):
            result = await create_stub_player(db, "Cooper Flagg")

        assert result.outcome == "blocked_existing"
        assert result.match == existing
        assert result.player_id is None
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_suffix_variant_blocks_creation(self) -> None:
        """A relaxed suffix variant matching an existing player must block creation."""
        db = MagicMock()
        lookup = _lookup_with_one_player(42, "Darius Acuff Jr.")

        with (
            patch(
                "app.services.player_mention_service._build_player_name_lookup",
                new=AsyncMock(return_value=lookup),
            ),
            patch(
                "app.services.player_mention_service._create_stub_player",
                new=AsyncMock(),
            ) as mock_create,
        ):
            result = await create_stub_player(db, "Darius Acuff")

        assert result.outcome == "blocked_existing"
        assert result.match is not None
        assert result.match.player_id == 42
        mock_create.assert_not_called()


class TestCreateStubPlayerAmbiguous:
    """Tests for the ambiguous outcome branch."""

    @pytest.mark.asyncio
    async def test_ambiguous_match_returns_candidates(self) -> None:
        """Multiple matches must block creation and return candidate list."""
        db = MagicMock()
        from app.services.player_mention_service import _normalized_name_key

        shared_key = _normalized_name_key(
            "John Smith",
            ignore_suffix=True,
            ignore_middle_initials=True,
        )
        lookup = _lookup_with_two_players_same_key(
            player_id_a=1,
            display_a="John A. Smith",
            player_id_b=2,
            display_b="John Smith Jr.",
            shared_relaxed_key=shared_key,
        )

        with (
            patch(
                "app.services.player_mention_service._build_player_name_lookup",
                new=AsyncMock(return_value=lookup),
            ),
            patch(
                "app.services.player_mention_service._create_stub_player",
                new=AsyncMock(),
            ) as mock_create,
        ):
            result = await create_stub_player(db, "John Smith")

        assert result.outcome == "ambiguous"
        assert result.candidates is not None
        assert len(result.candidates) == 2
        candidate_ids = {c.player_id for c in result.candidates}
        assert 1 in candidate_ids
        assert 2 in candidate_ids
        assert result.player_id is None
        mock_create.assert_not_called()


class TestCreateStubPlayerCreated:
    """Tests for the created outcome branch."""

    @pytest.mark.asyncio
    async def test_fresh_name_creates_stub(self) -> None:
        """A name with no existing match and ≥2 tokens must create a stub."""
        db = MagicMock()
        lookup = _empty_lookup()
        expected_match = PlayerMatch(
            player_id=7, display_name="Fresh Prospect", matched_via="stub_created"
        )

        with (
            patch(
                "app.services.player_mention_service._build_player_name_lookup",
                new=AsyncMock(return_value=lookup),
            ),
            patch(
                "app.services.player_mention_service._create_stub_player",
                new=AsyncMock(return_value=expected_match),
            ) as mock_create,
        ):
            result = await create_stub_player(db, "Fresh Prospect")

        assert result.outcome == "created"
        assert result.player_id == 7
        assert result.match is None
        mock_create.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_draft_year_forwarded_to_internal_creator(self) -> None:
        """draft_year keyword arg must be forwarded to _create_stub_player."""
        db = MagicMock()
        lookup = _empty_lookup()
        expected_match = PlayerMatch(
            player_id=8, display_name="Future Star", matched_via="stub_created"
        )

        with (
            patch(
                "app.services.player_mention_service._build_player_name_lookup",
                new=AsyncMock(return_value=lookup),
            ),
            patch(
                "app.services.player_mention_service._create_stub_player",
                new=AsyncMock(return_value=expected_match),
            ) as mock_create,
        ):
            result = await create_stub_player(db, "Future Star", draft_year=2027)

        assert result.outcome == "created"
        assert result.player_id == 8
        _args, _kwargs = mock_create.call_args
        assert _kwargs.get("draft_year") == 2027 or _args[2] == 2027

    @pytest.mark.asyncio
    async def test_three_token_name_is_accepted(self) -> None:
        """Names with middle names must pass the specificity guard."""
        db = MagicMock()
        lookup = _empty_lookup()
        expected_match = PlayerMatch(
            player_id=9, display_name="Walter A. Clayton", matched_via="stub_created"
        )

        with (
            patch(
                "app.services.player_mention_service._build_player_name_lookup",
                new=AsyncMock(return_value=lookup),
            ),
            patch(
                "app.services.player_mention_service._create_stub_player",
                new=AsyncMock(return_value=expected_match),
            ),
        ):
            result = await create_stub_player(db, "Walter A. Clayton")

        assert result.outcome == "created"
