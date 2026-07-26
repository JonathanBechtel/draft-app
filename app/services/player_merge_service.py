"""Atomic player-merge service.

Codifies the manual merge logic from ``scripts/top100/merge_players.py`` as a
safe, reusable service.  All public functions operate on an ``AsyncSession``
passed by the caller; the caller controls the transaction boundary.

Public API
----------
* :func:`preview_merge` — dry-run count of rows affected per table.
* :func:`merge_players` — atomic reassignment of all FK references from the
  discarded player to the survivor, plus alias creation and row deletion.
* :func:`count_inbound_references` — per-table inbound FK count, used by the
  safe-delete guard.
* :func:`find_duplicate_candidates` — near-duplicate candidates (excludes
  the player itself).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.player_mention_service import parse_player_name

if TYPE_CHECKING:
    from app.services.player_search_service import Candidate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TableStats:
    """Per-table row counts from a merge operation."""

    reassigned: int = 0
    deleted_conflict: int = 0


@dataclass(slots=True)
class MergeReport:
    """Result of a preview or executed merge.

    Attributes:
        keep_id: Primary key of the surviving player.
        discard_id: Primary key of the deleted player.
        per_table: Mapping of table name (and column for multi-column tables
            like ``player_similarity``) to :class:`TableStats`.
        alias_added: The ``display_name`` of the discard player that was
            inserted as an alias on the survivor, or ``None`` if the alias
            already existed (``ON CONFLICT DO NOTHING``).
    """

    keep_id: int
    discard_id: int
    per_table: dict[str, dict[str, int]]
    alias_added: str | None


# ---------------------------------------------------------------------------
# Child-table specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ChildTable:
    """Specification for a child table that references ``players_master``."""

    table: str
    player_column: str
    conflict_columns: tuple[str, ...] | None = None
    singleton_per_player: bool = False


# Tables to reassign (same set as the reference script, extended per ticket spec).
# Singletons: keep survivor's row, delete discard's.
# Conflict columns: if (discard_id + conflict_cols) already exist under keep_id,
#   delete the discard's row rather than violating the unique constraint.
_CHILD_TABLES: tuple[_ChildTable, ...] = (
    _ChildTable("player_aliases", "player_id", ("full_name",)),
    _ChildTable("player_lifecycle", "player_id", singleton_per_player=True),
    _ChildTable("player_status", "player_id", singleton_per_player=True),
    _ChildTable("player_content_mentions", "player_id", ("content_type", "content_id")),
    _ChildTable("player_college_stats", "player_id", ("season",)),
    _ChildTable("player_external_ids", "player_id", ("system", "external_id")),
    _ChildTable("player_bio_snapshots", "player_id"),
    _ChildTable(
        "player_metric_values",
        "player_id",
        ("snapshot_id", "metric_definition_id"),
    ),
    _ChildTable("combine_anthro", "player_id", ("season_id",)),
    _ChildTable("combine_agility", "player_id", ("season_id",)),
    _ChildTable("combine_shooting_results", "player_id", ("season_id",)),
    _ChildTable("big_board_consensus", "player_id", ("snapshot_id",)),
    # board_entries has uq_board_entries_board_player (board_id, player_id):
    # if both players sit on the same board, drop the discard's row instead of
    # reassigning into a uniqueness violation.
    _ChildTable("board_entries", "player_id", ("board_id",)),
    _ChildTable("news_items", "player_id"),
    _ChildTable("podcast_episodes", "player_id"),
    _ChildTable("player_image_assets", "player_id", ("snapshot_id",)),
    # player_enrichment_jobs FK -> players_master is non-cascade; reassign the
    # discard's queue rows so the final DELETE players_master does not violate it.
    _ChildTable("player_enrichment_jobs", "player_id"),
    # source_analytics.biggest_outlier_player_id is nullable — just NULL it out
    # for the discard rather than reassigning (it is not the primary FK column).
    _ChildTable("source_analytics_outlier", "biggest_outlier_player_id"),
    # --- #675 (backlog 4.4): draft, affiliation and Summer League FKs ---
    # Plain reassignments: none of these tables include the player FK in a
    # unique constraint (their uniqueness keys are game/source/team shaped).
    _ChildTable("draft_results", "player_id"),
    _ChildTable("player_affiliations", "player_id"),
    _ChildTable("summer_league_participation", "player_id"),
    _ChildTable("summer_league_player_game_logs", "player_id"),
    _ChildTable("summer_league_shot_events", "player_id"),
    _ChildTable("summer_league_source_players", "canonical_player_id"),
    _ChildTable("summer_league_player_resolution_reviews", "selected_player_id"),
    # PBP references players on three participant columns — one spec each,
    # mirroring the player_similarity two-column treatment.
    _ChildTable("summer_league_play_by_play_events", "person1_id"),
    _ChildTable("summer_league_play_by_play_events", "person2_id"),
    _ChildTable("summer_league_play_by_play_events", "person3_id"),
    # Desk projections: reassigned, not cascaded — cascade needs an ondelete
    # migration, and the rows stay valid for the surviving identity until the
    # next Desk tick rebuilds them. Grades are unique per (player, competition,
    # baseline_version), so a doubly-graded pair drops the discard's row.
    _ChildTable(
        "summer_league_desk_player_grades",
        "player_id",
        ("competition_id", "baseline_version"),
    ),
    _ChildTable("summer_league_desk_storylines", "subject_player_id"),
    _ChildTable("summer_league_desk_storylines", "subject_player_id_2"),
)

# player_similarity is handled separately because it appears on TWO columns.
_SIMILARITY_ANCHOR = _ChildTable(
    "player_similarity",
    "anchor_player_id",
    ("snapshot_id", "comparison_player_id", "dimension"),
)
_SIMILARITY_COMPARISON = _ChildTable(
    "player_similarity",
    "comparison_player_id",
    ("snapshot_id", "anchor_player_id", "dimension"),
)

# Tables that are ON DELETE CASCADE — they are automatically dropped when the
# discard row is deleted, so we never reassign them.
# player_embeddings, pending_image_previews, summer_league_player_seasons

# Synthetic spec names → real table, for specs the merge path special-cases.
_SPEC_TABLE_ALIASES = {"source_analytics_outlier": "source_analytics"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _fetch_display_name(conn: Any, player_id: int) -> str | None:
    """Return the display_name for a player, or None if not found."""
    row = (
        await conn.execute(
            text("SELECT display_name FROM players_master WHERE id = :player_id"),
            {"player_id": player_id},
        )
    ).fetchone()
    return str(row[0]) if row and row[0] else None


async def _count_rows(conn: Any, table: str, column: str, player_id: int) -> int:
    """Return the row count for (table, column) = player_id."""
    value = (
        await conn.execute(
            text(f"SELECT count(*) FROM {table} WHERE {column} = :player_id"),
            {"player_id": player_id},
        )
    ).scalar()
    return int(value or 0)


async def _merge_child_table(
    conn: Any,
    spec: _ChildTable,
    *,
    keep_id: int,
    discard_id: int,
    dry_run: bool,
) -> TableStats:
    """Merge one child table; return stats.

    Args:
        conn: An async SQLAlchemy connection (or session in execute mode).
        spec: Child-table specification.
        keep_id: Survivor player id.
        discard_id: Discarded player id.
        dry_run: When True, count affected rows but do not modify data.

    Returns:
        :class:`TableStats` with ``reassigned`` and ``deleted_conflict`` counts.
    """
    # Special-case: source_analytics.biggest_outlier_player_id is a nullable
    # reference — NULL it out for the discard rather than remapping to keep_id
    # (the outlier-player identity is only meaningful when fresh).
    if spec.table == "source_analytics_outlier":
        affected = int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM source_analytics "
                        "WHERE biggest_outlier_player_id = :discard_id"
                    ),
                    {"discard_id": discard_id},
                )
            ).scalar()
            or 0
        )
        if affected and not dry_run:
            await conn.execute(
                text(
                    "UPDATE source_analytics "
                    "SET biggest_outlier_player_id = NULL "
                    "WHERE biggest_outlier_player_id = :discard_id"
                ),
                {"discard_id": discard_id},
            )
        return TableStats(reassigned=affected, deleted_conflict=0)

    count = await _count_rows(conn, spec.table, spec.player_column, discard_id)
    if count == 0:
        return TableStats()

    # --- Singleton tables (player_lifecycle, player_status) ---
    if spec.singleton_per_player:
        keep_count = await _count_rows(conn, spec.table, spec.player_column, keep_id)
        if keep_count:
            # Survivor already has a row; delete the discard's row.
            if not dry_run:
                await conn.execute(
                    text(
                        f"DELETE FROM {spec.table} "
                        f"WHERE {spec.player_column} = :discard_id"
                    ),
                    {"discard_id": discard_id},
                )
            return TableStats(reassigned=0, deleted_conflict=count)
        # Survivor has no row; reassign the discard's row.
        if not dry_run:
            await conn.execute(
                text(
                    f"UPDATE {spec.table} SET {spec.player_column} = :keep_id "
                    f"WHERE {spec.player_column} = :discard_id"
                ),
                {"keep_id": keep_id, "discard_id": discard_id},
            )
        return TableStats(reassigned=count, deleted_conflict=0)

    # --- Tables with unique-conflict columns ---
    conflicts = 0
    if spec.conflict_columns:
        conflict_where = " AND ".join(
            f"d.{col} = k.{col}" for col in spec.conflict_columns
        )
        conflicts = int(
            (
                await conn.execute(
                    text(f"""
                        SELECT count(*)
                        FROM {spec.table} d
                        JOIN {spec.table} k
                          ON k.{spec.player_column} = :keep_id
                         AND {conflict_where}
                        WHERE d.{spec.player_column} = :discard_id
                    """),
                    {"keep_id": keep_id, "discard_id": discard_id},
                )
            ).scalar()
            or 0
        )
        if conflicts and not dry_run:
            await conn.execute(
                text(f"""
                    DELETE FROM {spec.table} d
                    USING {spec.table} k
                    WHERE d.{spec.player_column} = :discard_id
                      AND k.{spec.player_column} = :keep_id
                      AND {conflict_where}
                """),
                {"keep_id": keep_id, "discard_id": discard_id},
            )

    reassigned = count - conflicts
    if reassigned and not dry_run:
        await conn.execute(
            text(
                f"UPDATE {spec.table} SET {spec.player_column} = :keep_id "
                f"WHERE {spec.player_column} = :discard_id"
            ),
            {"keep_id": keep_id, "discard_id": discard_id},
        )

    return TableStats(reassigned=reassigned, deleted_conflict=conflicts)


async def _delete_similarity_self_links(
    conn: Any,
    *,
    keep_id: int,
    discard_id: int,
    dry_run: bool,
) -> int:
    """Delete similarity rows that would become self-links after reassignment.

    This covers three cases:
    - discard → keep (would become keep → keep after reassign anchor)
    - keep → discard (would become keep → keep after reassign comparison)
    - discard → discard (self-link from the discard)

    Args:
        conn: Database connection.
        keep_id: Survivor player id.
        discard_id: Discarded player id.
        dry_run: When True, count but do not delete.

    Returns:
        Number of rows counted (or deleted if not dry_run).
    """
    count = int(
        (
            await conn.execute(
                text("""
                    SELECT count(*)
                    FROM player_similarity
                    WHERE (anchor_player_id = :discard_id AND comparison_player_id = :keep_id)
                       OR (anchor_player_id = :keep_id AND comparison_player_id = :discard_id)
                       OR (anchor_player_id = :discard_id AND comparison_player_id = :discard_id)
                """),
                {"keep_id": keep_id, "discard_id": discard_id},
            )
        ).scalar()
        or 0
    )
    if count and not dry_run:
        await conn.execute(
            text("""
                DELETE FROM player_similarity
                WHERE (anchor_player_id = :discard_id AND comparison_player_id = :keep_id)
                   OR (anchor_player_id = :keep_id AND comparison_player_id = :discard_id)
                   OR (anchor_player_id = :discard_id AND comparison_player_id = :discard_id)
            """),
            {"keep_id": keep_id, "discard_id": discard_id},
        )
    return count


async def _ensure_alias(
    conn: Any,
    player_id: int,
    full_name: str,
    context: str,
) -> None:
    """Insert an alias for player_id from full_name, ON CONFLICT DO NOTHING.

    Mirrors ``_ensure_alias`` in the reference script but adapted for
    the async SQLAlchemy session API.

    Args:
        conn: Database connection.
        player_id: The player that will own the alias.
        full_name: The display name to store as an alias.
        context: Provenance string stored on the alias row.
    """
    parsed = parse_player_name(full_name)
    await conn.execute(
        text("""
            INSERT INTO player_aliases
                (player_id, full_name, first_name, middle_name, last_name, suffix, context, created_at)
            VALUES
                (:player_id, :full_name, :first_name, :middle_name, :last_name, :suffix, :context, now())
            ON CONFLICT DO NOTHING
        """),
        {
            "player_id": player_id,
            "full_name": full_name,
            "first_name": parsed.first_name or None,
            "middle_name": parsed.middle_name,
            "last_name": parsed.last_name,
            "suffix": parsed.suffix,
            "context": context,
        },
    )


async def _run_merge(
    db: AsyncSession,
    *,
    keep_id: int,
    discard_id: int,
    dry_run: bool,
) -> MergeReport:
    """Core merge logic shared by preview and execute paths.

    Args:
        db: Active async database session.
        keep_id: Survivor player id.
        discard_id: Discarded player id.
        dry_run: When True produce counts only, do not write.

    Returns:
        :class:`MergeReport` summarising per-table row counts.

    Raises:
        ValueError: If keep_id == discard_id, or either player is not found.
    """
    if keep_id == discard_id:
        raise ValueError("keep_id and discard_id must be different players")

    discard_name = await _fetch_display_name(db, discard_id)
    if discard_name is None:
        raise ValueError(f"Discard player {discard_id} not found")
    keep_name = await _fetch_display_name(db, keep_id)
    if keep_name is None:
        raise ValueError(f"Keep player {keep_id} not found")

    per_table: dict[str, dict[str, int]] = {}

    # 1. Handle player_similarity self-links first.
    self_link_count = await _delete_similarity_self_links(
        db, keep_id=keep_id, discard_id=discard_id, dry_run=dry_run
    )
    if self_link_count:
        per_table["player_similarity.self_links"] = {
            "reassigned": 0,
            "deleted_conflict": self_link_count,
        }

    # 2. Merge each child table.
    for spec in _CHILD_TABLES:
        stats = await _merge_child_table(
            db, spec, keep_id=keep_id, discard_id=discard_id, dry_run=dry_run
        )
        display_name = (
            "source_analytics.biggest_outlier_player_id"
            if spec.table == "source_analytics_outlier"
            else f"{spec.table}.{spec.player_column}"
        )
        if stats.reassigned or stats.deleted_conflict:
            per_table[display_name] = {
                "reassigned": stats.reassigned,
                "deleted_conflict": stats.deleted_conflict,
            }

    # 3. Merge player_similarity on both anchor and comparison columns.
    for sim_spec in (_SIMILARITY_ANCHOR, _SIMILARITY_COMPARISON):
        stats = await _merge_child_table(
            db, sim_spec, keep_id=keep_id, discard_id=discard_id, dry_run=dry_run
        )
        key = f"player_similarity.{sim_spec.player_column}"
        if stats.reassigned or stats.deleted_conflict:
            per_table[key] = {
                "reassigned": stats.reassigned,
                "deleted_conflict": stats.deleted_conflict,
            }

    # 4. Add alias from discard's display_name onto survivor.
    alias_added: str | None = None
    if not dry_run:
        await _ensure_alias(db, keep_id, discard_name, "admin_merge_discard")
        alias_added = discard_name
    else:
        alias_added = discard_name  # report what would be added

    # 5. Delete the discard player row (CASCADE handles embeddings + previews).
    if not dry_run:
        await db.execute(
            text("DELETE FROM players_master WHERE id = :discard_id"),
            {"discard_id": discard_id},
        )

    return MergeReport(
        keep_id=keep_id,
        discard_id=discard_id,
        per_table=per_table,
        alias_added=alias_added,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def preview_merge(
    db: AsyncSession,
    *,
    keep_id: int,
    discard_id: int,
) -> MergeReport:
    """Return a dry-run :class:`MergeReport` without modifying any data.

    Counts the rows that *would* be reassigned or deleted per table if
    :func:`merge_players` were called with the same arguments.  The database
    is not modified.

    Args:
        db: Active async database session.
        keep_id: Primary key of the player to keep (survivor).
        discard_id: Primary key of the player to discard.

    Returns:
        :class:`MergeReport` with per-table counts and the alias that would
        be created.

    Raises:
        ValueError: If ``keep_id == discard_id``, or either player is absent.
    """
    return await _run_merge(db, keep_id=keep_id, discard_id=discard_id, dry_run=True)


async def merge_players(
    db: AsyncSession,
    *,
    keep_id: int,
    discard_id: int,
    performed_by: int | None = None,
) -> MergeReport:
    """Atomically merge the discard player into the survivor.

    Reassigns every FK reference from ``discard_id`` to ``keep_id``, handles
    unique-constraint conflicts (delete the discard's conflicting rows) and
    singletons (keep survivor's row), inserts an alias from the discard's
    display_name onto the survivor, and deletes the discard ``PlayerMaster``
    row.  ``player_embeddings``, ``pending_image_previews`` and
    ``summer_league_player_seasons`` are dropped automatically via
    ``ON DELETE CASCADE``.

    The caller is responsible for wrapping this call in ``async with db.begin()``
    so that a mid-merge failure triggers a full rollback.

    Args:
        db: Active async database session.  The caller owns the transaction.
        keep_id: Primary key of the player to keep (survivor).
        discard_id: Primary key of the player to discard.
        performed_by: Optional admin user id for audit purposes (not yet
            persisted; logged for traceability).

    Returns:
        :class:`MergeReport` summarising per-table row counts.

    Raises:
        ValueError: If ``keep_id == discard_id``, or either player is absent.
    """
    logger.info(
        "merge_players: keep=%d discard=%d performed_by=%s",
        keep_id,
        discard_id,
        performed_by,
    )
    return await _run_merge(db, keep_id=keep_id, discard_id=discard_id, dry_run=False)


async def count_inbound_references(
    db: AsyncSession,
    player_id: int,
) -> dict[str, int]:
    """Return a per-table count of all inbound FK references for a player.

    Used by the safe-delete guard to block deletion of players that still
    have attached data.  Only counts non-CASCADE tables (CASCADE tables
    drop automatically and are not a deletion blocker).

    Args:
        db: Active async database session.
        player_id: Player to inspect.

    Returns:
        Dict mapping table.column label to row count (only non-zero entries
        are included).
    """
    # Derived from the classified merge specs so the safe-delete guard can
    # never drift from the merge path again (#675): every reassignable
    # (table, column) pair is by construction a non-CASCADE inbound FK.
    counts: dict[str, int] = {}
    for spec in (*_CHILD_TABLES, _SIMILARITY_ANCHOR, _SIMILARITY_COMPARISON):
        table = _SPEC_TABLE_ALIASES.get(spec.table, spec.table)
        n = await _count_rows(db, table, spec.player_column, player_id)
        if n:
            counts[f"{table}.{spec.player_column}"] = n
    return counts


async def find_duplicate_candidates(
    db: AsyncSession,
    player_id: int,
    k: int = 5,
) -> list[Candidate]:
    """Return near-duplicate candidates for a player, excluding itself.

    Delegates to :func:`~app.services.player_search_service.find_candidate_players`
    (hybrid trigram + vector search) using the player's ``display_name`` as
    the query string, then filters out the player itself from the results.

    Args:
        db: Active async database session.
        player_id: The player to find duplicates for.
        k: Maximum number of candidates to return (before excluding self).

    Returns:
        A list of up to *k* :class:`~app.services.player_search_service.Candidate`
        instances, excluding the player itself, ordered by descending score.

    Raises:
        ValueError: If the player is not found.
    """
    # Lazy import to avoid pulling in embedding_service (and its settings
    # dependency) at module-import time, keeping unit tests lightweight.
    from app.services.player_search_service import (  # noqa: PLC0415
        Candidate as _Candidate,
        find_candidate_players,
    )

    display_name = await _fetch_display_name(db, player_id)
    if display_name is None:
        raise ValueError(f"Player {player_id} not found")

    # Request k+1 to ensure we still have k results after removing self.
    candidates: list[_Candidate] = await find_candidate_players(
        db, display_name, k=k + 1
    )
    return [c for c in candidates if c.player_id != player_id][:k]
