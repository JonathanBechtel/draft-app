"""Gemini embedding service for player-name vector generation.

Provides three public helpers:

* ``embed_player`` — build a rich embed input from PlayerMaster fields and
  call Gemini; returns the raw float vector.
* ``embed_text`` — embed an arbitrary string (used by T4 vector search).
* ``embed_players_batch`` — embed a list of PlayerMaster rows in one API
  call (used by the backfill script).

The Gemini client is instantiated lazily on first use and is module-level so
it is reused across calls within a single process.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from google import genai
from google.genai import types

from app.config import settings
from app.schemas.players_master import PlayerMaster
from app.utils.network_guard import guard_network_io

logger = logging.getLogger(__name__)

# Hard ceiling on a single Gemini embedding call. Without it the underlying
# HTTP request can stall indefinitely, and because embeddings are generated
# inline during board extraction (one call per unresolved player name), a
# single hung call freezes the whole synchronous admin request — the
# "Extract Board" spinner then spins forever. Mirrors the wait_for guard on
# the generate_content call in board_extraction_service. A timeout surfaces
# as an exception so callers like ``find_candidate_players`` degrade to
# lexical-only matching instead of hanging.
_EMBED_TIMEOUT_SECONDS = 20

# Module-level client; instantiated on first use.
_client: Optional[genai.Client] = None


def _embed_config() -> types.EmbedContentConfig:
    """Build the embed config pinning output width to the table's vector size.

    ``gemini-embedding-001`` defaults to 3072 dims; we request
    ``settings.gemini_embedding_dim`` (768) so vectors fit the
    ``player_embeddings.embedding`` column. Cosine search is scale-invariant,
    so truncated-dimension vectors need no manual renormalization.

    ``task_type`` is pinned to ``settings.gemini_embedding_task_type``
    (SEMANTIC_SIMILARITY): name-to-name matching needs a symmetric task type
    on both the stored vector and the query, or short queries match on surface
    form rather than name content.
    """
    return types.EmbedContentConfig(
        output_dimensionality=settings.gemini_embedding_dim,
        task_type=settings.gemini_embedding_task_type,
    )


def _get_client() -> genai.Client:
    """Return (or lazily create) the shared Gemini client.

    Raises:
        RuntimeError: If neither ``GEMINI_API_KEY`` nor
            ``GEMINI_SUMMARIZATION_API_KEY`` is configured.
    """
    global _client
    if _client is None:
        api_key = settings.gemini_api_key or settings.gemini_summarization_api_key
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY or GEMINI_SUMMARIZATION_API_KEY must be set "
                "to use the embedding service."
            )
        _client = genai.Client(api_key=api_key)
    return _client


def build_player_embed_input(player: PlayerMaster) -> str:
    """Construct the text that will be embedded for a given player.

    Combining multiple biographical fields produces a richer semantic
    representation that helps disambiguate players who share a surname.
    Fields are included only when present; missing fields are silently
    omitted so the embed input degrades gracefully for stub players.

    Args:
        player: A ``PlayerMaster`` instance — need not be persisted yet.

    Returns:
        A single string suitable for passing to the embedding model.
    """
    parts: list[str] = []
    if player.display_name:
        parts.append(player.display_name)
    if player.school:
        parts.append(player.school)
    # position comes from a related table in production, but some players
    # carry it in bio snapshots; for the embed input we check if the
    # attribute exists at all (e.g., during tests or enriched records).
    position = getattr(player, "position", None)
    if position:
        parts.append(position)
    if player.birth_country:
        parts.append(player.birth_country)
    return " ".join(parts)


async def embed_player(
    player: PlayerMaster,
    *,
    client: Optional[genai.Client] = None,
) -> list[float]:
    """Generate an embedding vector for a single player.

    Builds the embed input from ``display_name``, ``school``,
    ``birth_country``, and (if present) ``position``, then calls Gemini.

    Args:
        player: Source player; may be a transient (not-yet-committed) object.
        client: Optional Gemini client override (injected in tests).

    Returns:
        768-dimensional float vector.

    Raises:
        RuntimeError: If the API key is not configured.
        Exception: Propagates any Gemini API error to the caller.
    """
    text = build_player_embed_input(player)
    return await embed_text(text, client=client)


async def embed_text(
    text: str,
    *,
    client: Optional[genai.Client] = None,
) -> list[float]:
    """Generate an embedding vector for an arbitrary text string.

    Used by T4 vector search to embed free-form query strings at
    resolution time.

    Args:
        text: The string to embed.  Must be non-empty.
        client: Optional Gemini client override (injected in tests).

    Returns:
        768-dimensional float vector.

    Raises:
        ValueError: If ``text`` is empty.
        RuntimeError: If the API key is not configured.
        Exception: Propagates any Gemini API error to the caller.
    """
    if not text.strip():
        raise ValueError("embed_text received an empty string.")

    _c = client or _get_client()
    guard_network_io("Gemini embedding request")
    try:
        response = await asyncio.wait_for(
            _c.aio.models.embed_content(
                model=settings.gemini_embedding_model,
                contents=[text],  # type: ignore[arg-type]
                config=_embed_config(),
            ),
            timeout=_EMBED_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Gemini embedding timed out after {_EMBED_TIMEOUT_SECONDS}s"
        ) from exc
    embeddings = response.embeddings
    if not embeddings or embeddings[0].values is None:
        raise RuntimeError(f"Gemini returned an empty embedding for text: {text!r}")
    return list(embeddings[0].values)


async def embed_players_batch(
    players: list[PlayerMaster],
    *,
    client: Optional[genai.Client] = None,
) -> list[list[float]]:
    """Embed a batch of players in a single Gemini API call.

    The Gemini embedding endpoint accepts a list of content strings and
    returns one embedding per string, preserving order.  This is used by
    the backfill script to avoid one-request-per-player overhead.

    Args:
        players: List of ``PlayerMaster`` instances to embed.
        client: Optional Gemini client override (injected in tests).

    Returns:
        List of 768-dimensional float vectors, one per input player, in
        the same order as ``players``.

    Raises:
        ValueError: If ``players`` is empty.
        RuntimeError: If the API key is not configured or Gemini returns
            fewer embeddings than expected.
    """
    if not players:
        raise ValueError("embed_players_batch requires at least one player.")

    texts = [build_player_embed_input(p) for p in players]
    _c = client or _get_client()
    guard_network_io("Gemini batch embedding request")
    try:
        response = await asyncio.wait_for(
            _c.aio.models.embed_content(
                model=settings.gemini_embedding_model,
                contents=texts,  # type: ignore[arg-type]
                config=_embed_config(),
            ),
            timeout=_EMBED_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Gemini embedding timed out after {_EMBED_TIMEOUT_SECONDS}s"
        ) from exc
    embeddings = response.embeddings
    if not embeddings or len(embeddings) != len(players):
        raise RuntimeError(
            f"Gemini returned {len(embeddings) if embeddings else 0} embeddings "
            f"for {len(players)} players."
        )
    result: list[list[float]] = []
    for emb in embeddings:
        if emb.values is None:
            raise RuntimeError("Gemini returned a null embedding vector.")
        result.append(list(emb.values))
    return result
