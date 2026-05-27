"""k-NN vector search and hybrid lexical+vector candidate search.

Provides two public search functions:

* ``find_similar_players`` — pure cosine-similarity k-NN search against the
  ``player_embeddings`` table.  Used by the admin compare tool (T9) where the
  caller supplies a semantically rich query string.
* ``find_candidate_players`` — hybrid search that blends a trigram lexical
  score (``pg_trgm`` similarity over ``players_master.display_name`` and
  ``player_aliases.full_name``) with the cosine-distance vector score.  Used
  by the resolution cascade (T5/T10) when exact and alias lookups both fail,
  because bare/short surnames (e.g. ``"mara"``) have low semantic signal but
  strong lexical overlap with the full name (``"Aday Mara"``).

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
2. Issuing ``SET search_path TO <current_path>, public`` via the raw
   connection before running the k-NN query.  asyncpg can execute ``SET``
   without a PREPARE because it is a utility statement.  The original path is
   restored in a ``finally`` block so the widened path does not leak to the
   next caller that reuses the pooled connection.
3. Running the k-NN SQL via the raw connection, which now resolves ``vector``
   correctly.

The vector literal is interpolated directly into the SQL string (never from raw
user input — the list comes from ``float()``-coerced embedding values) to avoid
parameter binding, which would itself require type OID resolution.

Hybrid blending strategy
------------------------
``find_candidate_players`` unions lexical (trigram) and vector candidates,
de-duplicates by ``player_id``, and orders by a combined score defined as::

    combined_score = max(lexical_score, vector_score)

Using ``max`` rather than a weighted average means a strong signal from
*either* modality wins, without penalising players who only appear in one
result set (e.g. a player with no embedding still surfaces via trigram).
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


# ---------------------------------------------------------------------------
# Trigram / lexical search
# ---------------------------------------------------------------------------

# Minimum pg_trgm similarity threshold for a row to appear in lexical results.
# A value of 0.1 is deliberately permissive: we want the candidate list to be
# broad (the resolution cascade never auto-resolves on score alone), and a
# bare surname like "mara" has ~0.25 similarity to "Aday Mara" which already
# clears the bar comfortably.
_TRGM_THRESHOLD: float = 0.1


async def find_lexical_players(
    db: AsyncSession,
    query: str,
    k: int = 5,
) -> list[Candidate]:
    """Return up to *k* players with the highest pg_trgm similarity to *query*.

    Searches both ``players_master.display_name`` and
    ``player_aliases.full_name``, returning one entry per player (the highest
    similarity score across both columns).  Rows below ``_TRGM_THRESHOLD``
    are excluded.

    This function executes the similarity SQL directly on the raw asyncpg
    connection (bypassing SQLAlchemy's PREPARE adapter) so that the
    ``similarity()`` function resolves correctly regardless of the active
    ``search_path``.  The ``pg_trgm`` functions live in ``public``, but
    schema-isolated integration tests restrict ``search_path`` to only the test
    schema.  We widen the path to include ``public`` before executing and
    restore it in ``finally`` — the same pattern used by ``find_similar_players``
    for the pgvector ``<=>`` operator.

    The ``query`` string is passed as a positional parameter (``$1``, ``$2``,
    ``$3``) rather than interpolated, so no raw user input reaches the SQL
    string itself.

    Args:
        db: An active async database session.
        query: The raw player name to search for (e.g. ``"mara"``).
        k: Maximum number of candidates to return.  Defaults to 5.

    Returns:
        A list of at most *k* :class:`Candidate` instances ordered by
        descending trigram similarity (highest first).
    """
    if not query.strip():
        return []

    # UNION of display_name similarity and alias similarity; take the max
    # score per player so a player who matches on both surfaces once with the
    # best score.  Parameters are positional ($1/$2/$3) to work with asyncpg's
    # native parameter binding (no PREPARE needed for SET statements).
    trgm_sql = (
        "SELECT"
        "    player_id,"
        "    display_name,"
        "    school,"
        "    MAX(sim) AS score"
        " FROM ("
        "    SELECT"
        "        pm.id           AS player_id,"
        "        pm.display_name AS display_name,"
        "        pm.school       AS school,"
        "        similarity(pm.display_name, $1) AS sim"
        "    FROM players_master pm"
        "    WHERE pm.display_name IS NOT NULL"
        "      AND similarity(pm.display_name, $1) >= $2"
        "    UNION ALL"
        "    SELECT"
        "        pm.id           AS player_id,"
        "        pm.display_name AS display_name,"
        "        pm.school       AS school,"
        "        similarity(pa.full_name, $1) AS sim"
        "    FROM player_aliases pa"
        "    JOIN players_master pm ON pm.id = pa.player_id"
        "    WHERE similarity(pa.full_name, $1) >= $2"
        " ) sub"
        " GROUP BY player_id, display_name, school"
        " ORDER BY score DESC"
        " LIMIT $3"
    )

    # --- Raw-connection path to avoid search_path hiding pg_trgm functions ---
    async_conn = await db.connection()
    raw_conn = await async_conn.get_raw_connection()
    asyncpg_conn: Any = raw_conn.driver_connection  # native asyncpg Connection

    path_record = await asyncpg_conn.fetchrow(
        "SELECT current_setting('search_path') AS sp"
    )
    current_path: str = path_record["sp"] if path_record else "public"
    widened = "public" not in current_path
    if widened:
        await asyncpg_conn.execute(f"SET search_path TO {current_path}, public")

    try:
        rows = await asyncpg_conn.fetch(trgm_sql, query, _TRGM_THRESHOLD, k)
    finally:
        if widened:
            await asyncpg_conn.execute(f"SET search_path TO {current_path}")
    # -------------------------------------------------------------------------

    lexical_candidates: list[Candidate] = []
    for row in rows:
        raw_score = float(row["score"])
        score = max(0.0, min(1.0, raw_score))
        lexical_candidates.append(
            Candidate(
                player_id=int(row["player_id"]),
                display_name=row["display_name"],
                school=row["school"],
                score=score,
            )
        )
    return lexical_candidates


# ---------------------------------------------------------------------------
# Hybrid candidate search (lexical + vector)
# ---------------------------------------------------------------------------


# Lexical matches at or above this trigram score lead the merged list, ahead
# of vector matches. Cross-modality scores are NOT comparable: cosine
# similarity for a short query is uniformly high (~0.8) for many unrelated
# players, which would bury a correct trigram substring hit (~0.5) under a
# max-of-scores ranking. A substring/typo hit is the more trustworthy signal,
# so it leads regardless of the vector score.
_LEXICAL_PRIORITY_THRESHOLD: float = 0.3


def _merge_candidates(
    lexical: list[Candidate],
    vector: list[Candidate],
) -> list[Candidate]:
    """Merge lexical and vector candidates with lexical priority.

    This is deliberately *not* a max-of-scores ranking — cross-modality scores
    are not comparable (see ``_LEXICAL_PRIORITY_THRESHOLD``). Order is:

    1. Strong lexical matches (score >= ``_LEXICAL_PRIORITY_THRESHOLD``), by
       descending lexical score;
    2. then vector matches, by descending cosine score;
    3. then any weak lexical matches.

    De-duplicated by ``player_id`` (first occurrence wins, keeping that
    candidate's originating-modality score and metadata).

    Args:
        lexical: Candidates from the trigram-similarity query.
        vector: Candidates from the cosine-distance k-NN query.

    Returns:
        De-duplicated, lexical-priority-ordered list of :class:`Candidate`.
    """
    ordered: list[Candidate] = []
    seen: set[int] = set()

    def _extend(cands: list[Candidate]) -> None:
        for c in cands:
            if c.player_id not in seen:
                seen.add(c.player_id)
                ordered.append(c)

    strong_lexical = sorted(
        (c for c in lexical if c.score >= _LEXICAL_PRIORITY_THRESHOLD),
        key=lambda c: c.score,
        reverse=True,
    )
    weak_lexical = sorted(
        (c for c in lexical if c.score < _LEXICAL_PRIORITY_THRESHOLD),
        key=lambda c: c.score,
        reverse=True,
    )
    vector_sorted = sorted(vector, key=lambda c: c.score, reverse=True)

    _extend(strong_lexical)
    _extend(vector_sorted)
    _extend(weak_lexical)
    return ordered


async def find_candidate_players(
    db: AsyncSession,
    query: str,
    k: int = 5,
) -> list[Candidate]:
    """Return up to *k* hybrid (lexical + vector) candidates for *query*.

    Runs a trigram lexical search (``find_lexical_players``) and a
    cosine-similarity vector search (``find_similar_players``) sequentially,
    then merges the results via :func:`_merge_candidates`.  The combined list
    is truncated to *k* entries.

    This is the function used by ``resolve_player`` in the board-extraction
    cascade when exact and alias lookups both fail.  It is more robust than
    pure vector search for short or bare-surname queries where semantic signal
    is weak.

    ``find_similar_players`` remains intact and is used directly by the admin
    compare tool, which always has richer, semantically meaningful queries.

    Args:
        db: An active async database session.
        query: The raw player name to search for (e.g. ``"mara"``).
        k: Maximum number of candidates to return after merging.  Defaults
            to 5.

    Returns:
        A list of at most *k* :class:`Candidate` instances ordered by
        descending combined score.
    """
    # Run sequentially: SQLAlchemy's AsyncSession is not concurrency-safe —
    # two coroutines awaiting the same session object at once causes
    # ``IllegalStateChangeError``.  Sequential execution is fine here; the
    # latency overhead is dominated by network round-trips to Postgres and
    # Gemini, not by Python scheduling.
    lexical = await find_lexical_players(db, query, k=k)
    vector = await find_similar_players(db, query, k=k)
    merged = _merge_candidates(lexical, vector)
    return merged[:k]
