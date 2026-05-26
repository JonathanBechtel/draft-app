"""k-NN vector search over player_embeddings.

Provides ``find_similar_players``, the single entry-point for cosine-similarity
search against the ``player_embeddings`` table.  Used by:

* The resolution cascade (T5) when exact and alias lookups both fail.
* The admin compare tool (T9) for free-form player-similarity queries.

No live Gemini calls occur inside this module; the embedding for an incoming
query string is produced by ``embedding_service.embed_text`` and then passed
directly to the pgvector ``<=>`` (cosine-distance) operator.

Implementation note — asyncpg and the vector type
--------------------------------------------------
asyncpg uses PREPARE for all SQL statements, and PREPARE resolves type OIDs at
parse time.  The ``vector`` type lives in ``public`` (where pgvector installs),
but schema-isolated integration tests set ``search_path`` to only the test
schema, hiding ``public``.  This causes any statement that references the
``vector`` type to fail during PREPARE — even ``'[…]'::vector`` literals have
their type resolved at parse time.

We work around this by:
1. Obtaining the raw asyncpg connection from the SQLAlchemy session.
2. Issuing ``SET LOCAL search_path TO <current_path>, public`` via the raw
   connection before running the k-NN query.  asyncpg can execute ``SET``
   without a PREPARE because it is a utility statement.  ``SET LOCAL`` limits
   the change to the current transaction so it is rolled back when the caller's
   transaction ends.
3. Running the k-NN SQL via the raw connection, which now resolves ``vector``
   correctly.

The vector literal is interpolated directly into the SQL string (never from raw
user input — the list comes from ``float()``-coerced embedding values) to avoid
parameter binding, which would itself require type OID resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.embedding_service import embed_text


@dataclass(frozen=True, slots=True)
class Candidate:
    """A player returned by a k-NN similarity search.

    Attributes:
        player_id: Primary key in ``players_master``.
        display_name: Human-readable player name.
        school: College/university affiliation, or ``None`` if not recorded.
        score: Cosine similarity in [0, 1]; higher means more similar.
            Computed as ``1 - cosine_distance`` where pgvector's ``<=>``
            operator returns the cosine distance.
    """

    player_id: int
    display_name: str | None
    school: str | None
    score: float


def _build_knn_sql(vec_str: str, k: int) -> str:
    """Return the k-NN query string with the vector literal inlined.

    The vector literal is interpolated rather than bound as a parameter
    because asyncpg resolves parameter types via PREPARE, and the ``vector``
    type OID may not be visible when ``search_path`` excludes ``public``.

    Args:
        vec_str: A pgvector-format string like ``"[0.1,0.2,...]"``.
        k: Maximum number of rows to return (inlined as an integer literal).

    Returns:
        A plain SQL string ready for ``asyncpg_conn.fetch()``.
    """
    return (
        f"SELECT"
        f"    pe.player_id,"
        f"    pm.display_name,"
        f"    pm.school,"
        f"    1.0 - (pe.embedding <=> '{vec_str}'::vector) AS score"
        f" FROM player_embeddings pe"
        f" JOIN players_master pm ON pm.id = pe.player_id"
        f" ORDER BY pe.embedding <=> '{vec_str}'::vector"
        f" LIMIT {k}"
    )


async def find_similar_players(
    db: AsyncSession,
    query: str,
    k: int = 5,
) -> list[Candidate]:
    """Return the top-K players most similar to ``query`` by cosine similarity.

    The query string is embedded via Gemini (``embed_text``) and the resulting
    vector is matched against the ``player_embeddings`` table using pgvector's
    cosine-distance operator (``<=>``).  Results are ordered from most to least
    similar and include a normalised ``score`` in [0, 1].

    This function executes the k-NN SQL directly on the raw asyncpg connection
    (bypassing SQLAlchemy's adapter) so that the ``vector`` type resolves
    correctly regardless of the active ``search_path``.  See the module
    docstring for the full rationale.

    Args:
        db: An active async database session.
        query: Free-form text to embed and search (e.g. a raw scraped name like
            ``"Cooper Flagg"`` or ``"flagg duke"``).
        k: Maximum number of candidates to return.  Defaults to 5.

    Returns:
        A list of at most *k* :class:`Candidate` instances ordered by
        descending ``score`` (highest similarity first).

    Raises:
        ValueError: If ``query`` is empty (propagated from ``embed_text``).
        RuntimeError: If the Gemini API is unreachable or returns a null vector
            (propagated from ``embed_text``).
    """
    query_vector: list[float] = await embed_text(query)

    # Build the pgvector literal "[f1,f2,...,fn]".  Values are float()-coerced
    # from the embedding service output and never contain raw user input.
    vec_str = "[" + ",".join(str(float(v)) for v in query_vector) + "]"

    # --- Raw-connection path to avoid asyncpg PREPARE + type-resolution issues ---
    async_conn = await db.connection()
    raw_conn = await async_conn.get_raw_connection()
    asyncpg_conn: Any = raw_conn.driver_connection  # native asyncpg Connection

    # Widen the search_path to include ``public`` (where the ``vector`` type
    # and its operators live) if it is not already there.  ``SET LOCAL`` is not
    # usable because the connection is not inside an asyncpg-managed transaction
    # here (Postgres ignores SET LOCAL outside a BEGIN/COMMIT block), and this
    # connection is returned to the pool afterwards — so we must restore the
    # original path in ``finally`` rather than leak the widened path to whoever
    # reuses the connection next.  In production ``public`` is already in the
    # default path, so the widen + restore is skipped entirely.
    path_record = await asyncpg_conn.fetchrow(
        "SELECT current_setting('search_path') AS sp"
    )
    current_path: str = path_record["sp"] if path_record else "public"
    widened = "public" not in current_path
    if widened:
        await asyncpg_conn.execute(f"SET search_path TO {current_path}, public")

    try:
        knn_sql = _build_knn_sql(vec_str, k)
        rows = await asyncpg_conn.fetch(knn_sql)
    finally:
        if widened:
            await asyncpg_conn.execute(f"SET search_path TO {current_path}")
    # -------------------------------------------------------------------------

    candidates: list[Candidate] = []
    for row in rows:
        player_id: int = row["player_id"]
        display_name: str | None = row["display_name"]
        school: str | None = row["school"]
        raw_score: float = float(row["score"])
        # Guard against floating-point noise pushing the score outside [0, 1].
        score = max(0.0, min(1.0, raw_score))
        candidates.append(
            Candidate(
                player_id=player_id,
                display_name=display_name,
                school=school,
                score=score,
            )
        )

    return candidates
