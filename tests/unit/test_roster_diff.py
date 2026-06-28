"""Unit tests for roster-diff classification and supersession decision logic.

Tests the pure ``classify_roster_diff`` helper from ``roster_ingest`` with no
database dependency. Covers the three classification buckets (added, unchanged,
cut) and the decision rules that drive assertion writes.
"""

from __future__ import annotations

import pytest

from app.services.summer_league.roster_ingest import classify_roster_diff


class TestDiffClassification:
    """test_diff_classification — added/unchanged/cut computed from two snapshots."""

    def test_all_new_roster(self) -> None:
        """Empty current roster → every incoming player is added."""
        added, unchanged, cut = classify_roster_diff(set(), {"A", "B", "C"})
        assert added == {"A", "B", "C"}
        assert unchanged == set()
        assert cut == set()

    def test_identical_roster(self) -> None:
        """Identical incoming snapshot → all unchanged, none added or cut."""
        added, unchanged, cut = classify_roster_diff({"A", "B"}, {"A", "B"})
        assert added == set()
        assert unchanged == {"A", "B"}
        assert cut == set()

    def test_mixed_add_unchanged_cut(self) -> None:
        """Partial overlap produces all three categories correctly."""
        added, unchanged, cut = classify_roster_diff({"A", "B"}, {"B", "C"})
        assert added == {"C"}
        assert unchanged == {"B"}
        assert cut == {"A"}

    def test_all_dropped(self) -> None:
        """Empty incoming roster → every current player is cut."""
        added, unchanged, cut = classify_roster_diff({"A", "B"}, set())
        assert added == set()
        assert unchanged == set()
        assert cut == {"A", "B"}

    def test_empty_both(self) -> None:
        """Both snapshots empty → all three sets are empty."""
        added, unchanged, cut = classify_roster_diff(set(), set())
        assert added == set()
        assert unchanged == set()
        assert cut == set()

    def test_sets_are_disjoint(self) -> None:
        """The three result sets are always mutually disjoint."""
        current = {"X", "Y", "Z"}
        incoming = {"Y", "Z", "W"}
        added, unchanged, cut = classify_roster_diff(current, incoming)
        assert not (added & unchanged)
        assert not (added & cut)
        assert not (unchanged & cut)

    def test_sets_union_covers_all(self) -> None:
        """Union of all three result sets covers the union of both input sets."""
        current = {"A", "B"}
        incoming = {"B", "C"}
        added, unchanged, cut = classify_roster_diff(current, incoming)
        assert added | unchanged | cut == current | incoming


class TestSupersessionDecisions:
    """test_supersession_decisions — classify_roster_diff drives assertion writes."""

    def test_cut_is_identified(self) -> None:
        """A dropped player must appear in the cut set (triggers CUT assertion)."""
        _, _, cut = classify_roster_diff({"player_1"}, set())
        assert "player_1" in cut

    def test_add_is_identified(self) -> None:
        """A new player must appear in the added set (triggers ANNOUNCED assertion)."""
        added, _, _ = classify_roster_diff(set(), {"player_2"})
        assert "player_2" in added

    def test_unchanged_triggers_no_assertion(self) -> None:
        """A retained player is in the unchanged set — not in added or cut."""
        added, unchanged, cut = classify_roster_diff({"player_3"}, {"player_3"})
        assert "player_3" in unchanged
        assert "player_3" not in added
        assert "player_3" not in cut

    def test_simultaneous_add_and_cut(self) -> None:
        """One player dropped and one added in the same pull are independent."""
        current = {"old_player"}
        incoming = {"new_player"}
        added, unchanged, cut = classify_roster_diff(current, incoming)
        assert "new_player" in added
        assert "old_player" in cut
        assert unchanged == set()

    def test_cut_person_not_in_incoming(self) -> None:
        """A cut player is never in the incoming set."""
        current = {"A", "B"}
        incoming = {"A", "C"}
        added, unchanged, cut = classify_roster_diff(current, incoming)
        # B is cut; it must not appear in added or unchanged
        assert "B" in cut
        assert "B" not in added
        assert "B" not in unchanged

    def test_added_person_not_in_current(self) -> None:
        """A newly added player was never in the current roster."""
        current = {"A"}
        incoming = {"A", "B"}
        added, unchanged, cut = classify_roster_diff(current, incoming)
        assert "B" in added
        # B was not in current so it cannot be in cut
        assert "B" not in cut
