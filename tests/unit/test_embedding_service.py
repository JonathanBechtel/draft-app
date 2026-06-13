"""Unit tests for ``app.services.embedding_service``.

No DB, no network.  The Gemini client is replaced with a lightweight mock
that returns canned vectors so tests run offline and are deterministic.

Tests also cover the backfill script's pure-logic helpers (player selection
query construction and batch dispatch) using a mocked embed function.
"""

from __future__ import annotations

import asyncio
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.players_master import PlayerMaster
from app.services.embedding_service import (
    build_player_embed_input,
    embed_player,
    embed_players_batch,
    embed_text,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_player(
    *,
    id: Optional[int] = 1,
    display_name: Optional[str] = "Cooper Flagg",
    school: Optional[str] = "Duke",
    birth_country: Optional[str] = "USA",
) -> PlayerMaster:
    """Construct a minimal PlayerMaster for testing without a DB session."""
    return PlayerMaster(
        id=id,
        display_name=display_name,
        school=school,
        birth_country=birth_country,
    )


def _make_mock_client(vector: list[float]) -> MagicMock:
    """Return a mock ``genai.Client`` whose async embed_content returns ``vector``."""
    content_embedding = MagicMock()
    content_embedding.values = vector

    embed_response = MagicMock()
    embed_response.embeddings = [content_embedding]

    mock_models = AsyncMock()
    mock_models.embed_content = AsyncMock(return_value=embed_response)

    mock_aio = MagicMock()
    mock_aio.models = mock_models

    client = MagicMock()
    client.aio = mock_aio
    return client


def _make_batch_mock_client(vectors: list[list[float]]) -> MagicMock:
    """Return a mock client that returns multiple embeddings for a batch call."""
    embeddings = []
    for v in vectors:
        emb = MagicMock()
        emb.values = v
        embeddings.append(emb)

    embed_response = MagicMock()
    embed_response.embeddings = embeddings

    mock_models = AsyncMock()
    mock_models.embed_content = AsyncMock(return_value=embed_response)

    mock_aio = MagicMock()
    mock_aio.models = mock_models

    client = MagicMock()
    client.aio = mock_aio
    return client


FAKE_VECTOR: list[float] = [0.1 * i for i in range(768)]


# ---------------------------------------------------------------------------
# build_player_embed_input
# ---------------------------------------------------------------------------


class TestBuildPlayerEmbedInput:
    """Tests for the pure embed-input construction helper."""

    def test_all_fields_present(self) -> None:
        """When display_name, school, and birth_country are all set, all appear
        in the embed input in that order.
        """
        player = _make_player(
            display_name="Cooper Flagg",
            school="Duke",
            birth_country="USA",
        )
        result = build_player_embed_input(player)
        assert "Cooper Flagg" in result
        assert "Duke" in result
        assert "USA" in result
        # Order: name first
        assert result.startswith("Cooper Flagg")

    def test_missing_school_omitted(self) -> None:
        """When ``school`` is None, the embed input degrades gracefully."""
        player = _make_player(school=None)
        result = build_player_embed_input(player)
        assert "Cooper Flagg" in result
        assert "None" not in result

    def test_missing_birth_country_omitted(self) -> None:
        """When ``birth_country`` is None, it is silently omitted."""
        player = _make_player(birth_country=None)
        result = build_player_embed_input(player)
        assert "Cooper Flagg" in result
        assert "None" not in result

    def test_stub_player_only_name(self) -> None:
        """A stub player with only display_name returns just the name."""
        player = _make_player(school=None, birth_country=None)
        result = build_player_embed_input(player)
        assert result == "Cooper Flagg"

    def test_fully_empty_player_returns_empty_string(self) -> None:
        """A player with no fields at all returns an empty string."""
        player = _make_player(display_name=None, school=None, birth_country=None)
        result = build_player_embed_input(player)
        assert result == ""

    def test_position_not_on_player_master(self) -> None:
        """``PlayerMaster`` has no ``position`` field; getattr returns None and
        the embed input does not contain a bare 'None' string.
        """
        player = _make_player()
        result = build_player_embed_input(player)
        # position is not a PlayerMaster field, so it is silently omitted.
        assert "None" not in result
        # The three available fields should still be present.
        assert "Cooper Flagg" in result


# ---------------------------------------------------------------------------
# embed_text
# ---------------------------------------------------------------------------


class TestEmbedText:
    """Tests for the free-form text embedding helper."""

    @pytest.mark.asyncio
    async def test_returns_mocked_vector(self) -> None:
        """``embed_text`` returns exactly the vector provided by the mock client."""
        client = _make_mock_client(FAKE_VECTOR)
        result = await embed_text("Cooper Flagg Duke USA", client=client)
        assert result == FAKE_VECTOR

    @pytest.mark.asyncio
    async def test_calls_correct_model(self) -> None:
        """The configured model name is passed to the Gemini embed_content call."""
        client = _make_mock_client(FAKE_VECTOR)
        from app.config import settings

        await embed_text("some player text", client=client)
        call_kwargs = client.aio.models.embed_content.call_args.kwargs
        assert call_kwargs["model"] == settings.gemini_embedding_model

    @pytest.mark.asyncio
    async def test_raises_on_empty_string(self) -> None:
        """Passing an empty string raises ``ValueError`` before any API call."""
        client = _make_mock_client(FAKE_VECTOR)
        with pytest.raises(ValueError, match="empty string"):
            await embed_text("   ", client=client)
        client.aio.models.embed_content.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_on_null_embedding(self) -> None:
        """When Gemini returns a null ``values`` field, ``RuntimeError`` is raised."""
        content_embedding = MagicMock()
        content_embedding.values = None
        embed_response = MagicMock()
        embed_response.embeddings = [content_embedding]
        mock_models = AsyncMock()
        mock_models.embed_content = AsyncMock(return_value=embed_response)
        mock_aio = MagicMock()
        mock_aio.models = mock_models
        client = MagicMock()
        client.aio = mock_aio

        with pytest.raises(RuntimeError, match="empty embedding"):
            await embed_text("some text", client=client)

    @pytest.mark.asyncio
    async def test_raises_on_empty_embeddings_list(self) -> None:
        """When Gemini returns an empty embeddings list, ``RuntimeError`` is raised."""
        embed_response = MagicMock()
        embed_response.embeddings = []
        mock_models = AsyncMock()
        mock_models.embed_content = AsyncMock(return_value=embed_response)
        mock_aio = MagicMock()
        mock_aio.models = mock_models
        client = MagicMock()
        client.aio = mock_aio

        with pytest.raises(RuntimeError, match="empty embedding"):
            await embed_text("some text", client=client)

    @pytest.mark.asyncio
    async def test_raises_timeout_when_call_stalls(self) -> None:
        """A stalled embed_content must raise (not hang) so callers can degrade.

        Regression guard for the "Extract Board" spinner that spun forever: a
        single hung embedding call (one per unresolved player name) froze the
        whole synchronous extraction request. ``embed_text`` now bounds the
        call and surfaces a timeout as ``RuntimeError`` within the deadline.
        """

        async def _never_returns(*args: object, **kwargs: object) -> object:
            await asyncio.sleep(10)
            raise AssertionError("embed_content should have been cancelled")

        mock_models = MagicMock()
        mock_models.embed_content = _never_returns
        mock_aio = MagicMock()
        mock_aio.models = mock_models
        client = MagicMock()
        client.aio = mock_aio

        with patch("app.services.embedding_service._EMBED_TIMEOUT_SECONDS", 0.05):
            with pytest.raises(RuntimeError, match="timed out"):
                await asyncio.wait_for(
                    embed_text("Aday Mara", client=client), timeout=2.0
                )


# ---------------------------------------------------------------------------
# embed_player
# ---------------------------------------------------------------------------


class TestEmbedPlayer:
    """Tests for the per-player embedding helper."""

    @pytest.mark.asyncio
    async def test_embed_input_contains_name_school_country(self) -> None:
        """The text sent to Gemini must contain name, school, and country."""
        client = _make_mock_client(FAKE_VECTOR)
        player = _make_player(
            display_name="Dylan Harper",
            school="Rutgers",
            birth_country="USA",
        )
        await embed_player(player, client=client)

        call_kwargs = client.aio.models.embed_content.call_args.kwargs
        contents = call_kwargs["contents"]
        assert len(contents) == 1
        embed_input = contents[0]
        assert "Dylan Harper" in embed_input
        assert "Rutgers" in embed_input
        assert "USA" in embed_input

    @pytest.mark.asyncio
    async def test_returns_mocked_vector(self) -> None:
        """The vector returned by embed_player matches the mock's response."""
        client = _make_mock_client(FAKE_VECTOR)
        player = _make_player()
        result = await embed_player(player, client=client)
        assert result == FAKE_VECTOR

    @pytest.mark.asyncio
    async def test_omits_none_fields_from_embed_input(self) -> None:
        """None-valued fields must not appear as the string 'None' in the input."""
        client = _make_mock_client(FAKE_VECTOR)
        player = _make_player(school=None, birth_country=None)
        await embed_player(player, client=client)

        call_kwargs = client.aio.models.embed_content.call_args.kwargs
        embed_input = call_kwargs["contents"][0]
        assert "None" not in embed_input


# ---------------------------------------------------------------------------
# embed_players_batch
# ---------------------------------------------------------------------------


class TestEmbedPlayersBatch:
    """Tests for the batched embedding helper."""

    @pytest.mark.asyncio
    async def test_returns_one_vector_per_player(self) -> None:
        """The returned list length matches the number of input players."""
        vectors = [[float(i)] * 768 for i in range(3)]
        client = _make_batch_mock_client(vectors)
        players = [_make_player(id=i, display_name=f"Player {i}") for i in range(3)]
        result = await embed_players_batch(players, client=client)
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_vectors_match_players_in_order(self) -> None:
        """Each returned vector corresponds to the player at the same index."""
        vectors = [[float(i)] * 768 for i in range(3)]
        client = _make_batch_mock_client(vectors)
        players = [_make_player(id=i, display_name=f"Player {i}") for i in range(3)]
        result = await embed_players_batch(players, client=client)
        for i, vec in enumerate(result):
            assert vec == vectors[i], f"Mismatch at index {i}"

    @pytest.mark.asyncio
    async def test_raises_on_empty_list(self) -> None:
        """Passing an empty player list raises ``ValueError``."""
        client = _make_batch_mock_client([])
        with pytest.raises(ValueError, match="at least one player"):
            await embed_players_batch([], client=client)

    @pytest.mark.asyncio
    async def test_raises_on_count_mismatch(self) -> None:
        """If Gemini returns fewer embeddings than players, ``RuntimeError`` is raised."""
        # Return only 1 vector for 2 players
        vectors = [FAKE_VECTOR]
        client = _make_batch_mock_client(vectors)
        players = [_make_player(id=i) for i in range(2)]
        with pytest.raises(RuntimeError, match="embeddings"):
            await embed_players_batch(players, client=client)

    @pytest.mark.asyncio
    async def test_single_api_call_for_batch(self) -> None:
        """The entire batch is sent in one Gemini API call, not one call per player."""
        vectors = [FAKE_VECTOR] * 5
        client = _make_batch_mock_client(vectors)
        players = [_make_player(id=i, display_name=f"P{i}") for i in range(5)]
        await embed_players_batch(players, client=client)
        assert client.aio.models.embed_content.call_count == 1

    @pytest.mark.asyncio
    async def test_all_player_texts_in_single_call(self) -> None:
        """All player embed inputs are passed as the ``contents`` list in one call."""
        vectors = [FAKE_VECTOR] * 3
        client = _make_batch_mock_client(vectors)
        players = [
            _make_player(id=1, display_name="Cooper Flagg", school="Duke"),
            _make_player(id=2, display_name="Dylan Harper", school="Rutgers"),
            _make_player(id=3, display_name="Tre Johnson", school="Texas"),
        ]
        await embed_players_batch(players, client=client)
        call_kwargs = client.aio.models.embed_content.call_args.kwargs
        contents = call_kwargs["contents"]
        assert len(contents) == 3
        assert any("Cooper Flagg" in c for c in contents)
        assert any("Dylan Harper" in c for c in contents)
        assert any("Tre Johnson" in c for c in contents)


# ---------------------------------------------------------------------------
# Backfill script — pure logic (mocked embed function)
# ---------------------------------------------------------------------------


class TestBackfillLogic:
    """Tests for the backfill script using a mocked embed function and DB."""

    @pytest.mark.asyncio
    async def test_backfill_calls_embed_for_each_player(self) -> None:
        """backfill() calls the embed function once per batch of missing players.

        We mock both the DB fetch and the embed function to verify that
        the backfill orchestration wires them correctly.
        """
        from scripts.backfill_player_embeddings import backfill

        players = [
            _make_player(id=i, display_name=f"Player {i}", school="Duke")
            for i in range(5)
        ]

        # Mock embed_fn that records calls and returns fake vectors.
        embed_calls: list[list[PlayerMaster]] = []

        async def fake_embed(batch: list[PlayerMaster]) -> list[list[float]]:
            embed_calls.append(batch)
            return [FAKE_VECTOR for _ in batch]

        # Patch the DB fetch and the engine/session so no real DB is needed.
        with (
            patch(
                "scripts.backfill_player_embeddings.fetch_players_missing_embeddings",
                new=AsyncMock(return_value=players),
            ),
            patch(
                "scripts.backfill_player_embeddings.create_async_engine",
            ) as mock_engine_cls,
            patch(
                "scripts.backfill_player_embeddings.async_sessionmaker",
            ) as mock_factory_cls,
        ):
            # Minimal engine / session mocks that satisfy the async context managers.
            mock_engine = AsyncMock()
            mock_engine.dispose = AsyncMock()
            mock_engine_cls.return_value = mock_engine

            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)

            # db.begin() used as async context manager for each batch.
            mock_txn = AsyncMock()
            mock_txn.__aenter__ = AsyncMock(return_value=mock_txn)
            mock_txn.__aexit__ = AsyncMock(return_value=False)
            mock_session.begin = MagicMock(return_value=mock_txn)
            mock_session.execute = AsyncMock()

            mock_factory = MagicMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_factory_cls.return_value = mock_factory

            total = await backfill(batch_size=3, dry_run=False, embed_fn=fake_embed)

        # 5 players / batch_size=3 → 2 batches → 5 rows written.
        assert total == 5
        # Two embed calls: batch of 3, then batch of 2.
        assert len(embed_calls) == 2
        assert len(embed_calls[0]) == 3
        assert len(embed_calls[1]) == 2

    @pytest.mark.asyncio
    async def test_dry_run_skips_embed_and_writes(self) -> None:
        """In dry-run mode, no embed calls are made and 0 rows are written."""
        from scripts.backfill_player_embeddings import backfill

        players = [_make_player(id=i) for i in range(10)]
        embed_calls: list[object] = []

        async def fake_embed(batch: list[PlayerMaster]) -> list[list[float]]:
            embed_calls.append(batch)
            return [FAKE_VECTOR for _ in batch]

        with (
            patch(
                "scripts.backfill_player_embeddings.fetch_players_missing_embeddings",
                new=AsyncMock(return_value=players),
            ),
            patch(
                "scripts.backfill_player_embeddings.create_async_engine",
            ) as mock_engine_cls,
            patch(
                "scripts.backfill_player_embeddings.async_sessionmaker",
            ) as mock_factory_cls,
        ):
            mock_engine = AsyncMock()
            mock_engine.dispose = AsyncMock()
            mock_engine_cls.return_value = mock_engine

            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)

            mock_factory = MagicMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_factory_cls.return_value = mock_factory

            total = await backfill(dry_run=True, embed_fn=fake_embed)

        assert total == 0
        assert embed_calls == []

    @pytest.mark.asyncio
    async def test_embed_failure_skips_batch_gracefully(self) -> None:
        """An embed API failure logs and skips the failing batch without crashing."""
        from scripts.backfill_player_embeddings import backfill

        players = [_make_player(id=i) for i in range(4)]
        call_count = 0

        async def flaky_embed(batch: list[PlayerMaster]) -> list[list[float]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Simulated Gemini API failure")
            return [FAKE_VECTOR for _ in batch]

        with (
            patch(
                "scripts.backfill_player_embeddings.fetch_players_missing_embeddings",
                new=AsyncMock(return_value=players),
            ),
            patch(
                "scripts.backfill_player_embeddings.create_async_engine",
            ) as mock_engine_cls,
            patch(
                "scripts.backfill_player_embeddings.async_sessionmaker",
            ) as mock_factory_cls,
        ):
            mock_engine = AsyncMock()
            mock_engine.dispose = AsyncMock()
            mock_engine_cls.return_value = mock_engine

            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)

            mock_txn = AsyncMock()
            mock_txn.__aenter__ = AsyncMock(return_value=mock_txn)
            mock_txn.__aexit__ = AsyncMock(return_value=False)
            mock_session.begin = MagicMock(return_value=mock_txn)
            mock_session.execute = AsyncMock()

            mock_factory = MagicMock()
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_factory_cls.return_value = mock_factory

            # batch_size=3: first batch of 3 fails, second batch of 1 succeeds.
            total = await backfill(batch_size=3, dry_run=False, embed_fn=flaky_embed)

        # Only the second batch (1 player) should be written.
        assert total == 1
