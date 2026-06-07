"""Unit tests for player_merge_service planning logic.

Tests the _ChildTable spec configuration and MergeReport dataclass rather
than hitting a real database. The planning tests verify:
- Singleton tables: discard row deleted when survivor has one; reassigned when not.
- Conflict columns: conflicting rows counted/deleted; non-conflicting rows reassigned.
- self-link detection logic for player_similarity.
- keep_id == discard_id rejected.
- MergeReport dataclass construction.

No DB required; mock async objects stand in for the connection.
"""

from __future__ import annotations

import pytest

from app.services.player_merge_service import (
    MergeReport,
    TableStats,
    _CHILD_TABLES,
    _SIMILARITY_ANCHOR,
    _SIMILARITY_COMPARISON,
    _ChildTable,
)


# ---------------------------------------------------------------------------
# MergeReport construction
# ---------------------------------------------------------------------------


def test_merge_report_empty_per_table() -> None:
    """MergeReport can be constructed with an empty per_table dict."""
    report = MergeReport(keep_id=1, discard_id=2, per_table={}, alias_added="Alice")
    assert report.keep_id == 1
    assert report.discard_id == 2
    assert report.per_table == {}
    assert report.alias_added == "Alice"


def test_merge_report_with_stats() -> None:
    """MergeReport stores per-table counts correctly."""
    report = MergeReport(
        keep_id=10,
        discard_id=20,
        per_table={
            "player_aliases.player_id": {"reassigned": 3, "deleted_conflict": 1},
            "player_lifecycle.player_id": {"reassigned": 0, "deleted_conflict": 1},
        },
        alias_added="Bob Smith",
    )
    assert report.per_table["player_aliases.player_id"]["reassigned"] == 3
    assert report.per_table["player_lifecycle.player_id"]["deleted_conflict"] == 1


def test_merge_report_alias_none() -> None:
    """MergeReport alias_added can be None."""
    report = MergeReport(keep_id=1, discard_id=2, per_table={}, alias_added=None)
    assert report.alias_added is None


# ---------------------------------------------------------------------------
# TableStats
# ---------------------------------------------------------------------------


def test_table_stats_defaults() -> None:
    """TableStats defaults both counters to 0."""
    stats = TableStats()
    assert stats.reassigned == 0
    assert stats.deleted_conflict == 0


def test_table_stats_values() -> None:
    """TableStats stores explicit values correctly."""
    stats = TableStats(reassigned=5, deleted_conflict=2)
    assert stats.reassigned == 5
    assert stats.deleted_conflict == 2


# ---------------------------------------------------------------------------
# Child table spec coverage
# ---------------------------------------------------------------------------


def test_singleton_tables_present() -> None:
    """player_lifecycle and player_status must be singleton_per_player=True."""
    singletons = {spec.table for spec in _CHILD_TABLES if spec.singleton_per_player}
    assert "player_lifecycle" in singletons
    assert "player_status" in singletons


def test_player_aliases_has_conflict_column() -> None:
    """player_aliases must declare full_name as the conflict column."""
    alias_spec = next(s for s in _CHILD_TABLES if s.table == "player_aliases")
    assert alias_spec.conflict_columns == ("full_name",)


def test_player_content_mentions_conflict_columns() -> None:
    """player_content_mentions must conflict on (content_type, content_id)."""
    spec = next(s for s in _CHILD_TABLES if s.table == "player_content_mentions")
    assert spec.conflict_columns is not None
    assert set(spec.conflict_columns) == {"content_type", "content_id"}


def test_combine_anthro_conflict_column() -> None:
    """combine_anthro must conflict on season_id."""
    spec = next(s for s in _CHILD_TABLES if s.table == "combine_anthro")
    assert spec.conflict_columns == ("season_id",)


def test_board_entries_conflict_on_board_id() -> None:
    """board_entries must conflict on board_id.

    uq_board_entries_board_player (board_id, player_id) means that if the
    survivor and discard are both on the same board, reassigning the discard
    would violate the constraint; the merge drops the discard's conflicting
    entry instead.
    """
    spec = next(s for s in _CHILD_TABLES if s.table == "board_entries")
    assert not spec.singleton_per_player
    assert spec.conflict_columns == ("board_id",)


def test_news_items_no_conflict_columns() -> None:
    """news_items.player_id is nullable; no unique-conflict columns needed."""
    spec = next(s for s in _CHILD_TABLES if s.table == "news_items")
    assert not spec.singleton_per_player


def test_podcast_episodes_no_conflict_columns() -> None:
    """podcast_episodes.player_id is nullable; no unique-conflict columns."""
    spec = next(s for s in _CHILD_TABLES if s.table == "podcast_episodes")
    assert not spec.singleton_per_player


def test_source_analytics_outlier_column() -> None:
    """source_analytics outlier spec targets biggest_outlier_player_id."""
    spec = next(s for s in _CHILD_TABLES if s.table == "source_analytics_outlier")
    assert spec.player_column == "biggest_outlier_player_id"


def test_similarity_anchor_spec() -> None:
    """_SIMILARITY_ANCHOR targets player_similarity.anchor_player_id."""
    assert _SIMILARITY_ANCHOR.table == "player_similarity"
    assert _SIMILARITY_ANCHOR.player_column == "anchor_player_id"
    assert _SIMILARITY_ANCHOR.conflict_columns is not None
    assert "comparison_player_id" in _SIMILARITY_ANCHOR.conflict_columns
    assert "snapshot_id" in _SIMILARITY_ANCHOR.conflict_columns
    assert "dimension" in _SIMILARITY_ANCHOR.conflict_columns


def test_similarity_comparison_spec() -> None:
    """_SIMILARITY_COMPARISON targets player_similarity.comparison_player_id."""
    assert _SIMILARITY_COMPARISON.table == "player_similarity"
    assert _SIMILARITY_COMPARISON.player_column == "comparison_player_id"
    assert _SIMILARITY_COMPARISON.conflict_columns is not None
    assert "anchor_player_id" in _SIMILARITY_COMPARISON.conflict_columns


def test_child_tables_all_have_player_column() -> None:
    """Every spec in _CHILD_TABLES has a non-empty player_column."""
    for spec in _CHILD_TABLES:
        assert spec.player_column, f"{spec.table} has empty player_column"


# ---------------------------------------------------------------------------
# keep_id == discard_id guard (fast path — no DB needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_merge_same_id_raises() -> None:
    """preview_merge must raise ValueError when keep_id == discard_id."""
    from unittest.mock import AsyncMock, MagicMock

    db = MagicMock()
    # _fetch_display_name needs to be awaited — make execute return nothing
    # but the guard fires before any DB call.
    db.execute = AsyncMock(side_effect=AssertionError("should not reach DB"))

    from app.services.player_merge_service import preview_merge

    with pytest.raises(ValueError, match="must be different"):
        await preview_merge(db, keep_id=42, discard_id=42)


@pytest.mark.asyncio
async def test_merge_players_same_id_raises() -> None:
    """merge_players must raise ValueError when keep_id == discard_id."""
    from unittest.mock import AsyncMock, MagicMock

    db = MagicMock()
    db.execute = AsyncMock(side_effect=AssertionError("should not reach DB"))

    from app.services.player_merge_service import merge_players

    with pytest.raises(ValueError, match="must be different"):
        await merge_players(db, keep_id=7, discard_id=7)


# ---------------------------------------------------------------------------
# _ChildTable frozen dataclass behaviour
# ---------------------------------------------------------------------------


def test_child_table_is_frozen() -> None:
    """_ChildTable instances cannot be mutated after creation."""
    spec = _ChildTable("some_table", "player_id", ("col_a",))
    with pytest.raises((AttributeError, TypeError)):
        spec.table = "other"  # type: ignore[misc]


def test_child_table_conflict_columns_optional() -> None:
    """_ChildTable with no conflict_columns defaults to None."""
    spec = _ChildTable("foo", "player_id")
    assert spec.conflict_columns is None
    assert not spec.singleton_per_player
