"""Child-table registry for the player merge — which FKs to ``players_master`` move.

Extracted from ``player_merge_service`` so the registry can grow without pushing an
already-oversized module further over the file-size ratchet
(``docs/plans/programmatic-code-discipline.md`` §1.4). It is declarative data, not merge
logic, and it is what ``tests/unit/test_player_merge_fk_coverage.py`` reflects over — so a
module of its own is the honest home for it.

Every column with an FK to ``players_master`` must be *classified*: registered here for
reassignment, declared ``ondelete="CASCADE"`` so its rows die with the discarded identity, or
blanked via a sentinel spec (see ``source_analytics_outlier``). That coverage is enforced by
the FK-coverage test — see ``docs/plans/programmatic-code-discipline.md`` §3.4.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    # --- Backbone + Summer League -------------------------------------------------
    # These FKs accumulated while this list was maintained by hand, so merging a player
    # holding any of this data hard-failed on a RESTRICT FK. Every one is a canonical
    # assertion about the player (or a projection keyed on them), so all reassign to the
    # survivor; none are cascade-delete. Coverage is enforced by
    # tests/unit/test_player_merge_fk_coverage.py.
    #
    # Only desk_player_grades carries a unique constraint containing the player column, so
    # it is the only one that can collide on reassignment; the rest key on game/event ids.
    _ChildTable("draft_results", "player_id"),
    _ChildTable("player_affiliations", "player_id"),
    _ChildTable("summer_league_participation", "player_id"),
    _ChildTable("summer_league_player_game_logs", "player_id"),
    _ChildTable("summer_league_shot_events", "player_id"),
    _ChildTable("summer_league_source_players", "canonical_player_id"),
    _ChildTable("summer_league_player_resolution_reviews", "selected_player_id"),
    # Play-by-play names up to three participants per event; each column is reassigned
    # independently, and an event naming the discard twice simply lands on the survivor
    # in both slots.
    _ChildTable("summer_league_play_by_play_events", "person1_id"),
    _ChildTable("summer_league_play_by_play_events", "person2_id"),
    _ChildTable("summer_league_play_by_play_events", "person3_id"),
    # UNIQUE (player_id, competition_id, baseline_version): if the survivor already holds
    # a grade for the same competition and baseline, drop the discard's rather than
    # violating it. Grades are a regenerable projection, so losing the duplicate is fine.
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

# Synthetic spec names → real table, for specs the merge path special-cases
# (the sentinel's table name is not a real table, so anything iterating the
# specs against the database must map it back first).
_SPEC_TABLE_ALIASES = {"source_analytics_outlier": "source_analytics"}

# Tables that are ON DELETE CASCADE — they are automatically dropped when the
# discard row is deleted, so we never reassign them.
# player_embeddings, pending_image_previews, summer_league_player_seasons
# (SL player-season metric rows are a regenerable projection; the next metrics
# rebuild recreates them under the surviving identity).
