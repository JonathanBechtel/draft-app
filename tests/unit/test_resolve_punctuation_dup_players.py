"""Tests for duplicate-player classification.

The rule these protect: **normalized-name equality is not evidence of a duplicate.** Stripping
punctuation collapses ``Ron Harper`` onto ``Ron Harper Jr.`` and ``Scotty Pippen`` onto
``Scotty Pippen Jr.`` — father/son pairs that both exist in production. This repo has already
suffered one namesake merge (Basketball-Reference first-initial matching contaminated Derek
Harper's bio with Dylan Harper's), so the classifier must decline anything it cannot prove.

Every test here is written from a real production or dev group.
"""

from __future__ import annotations

import pytest

from tests.unit._script_loader import load_script


resolver = load_script("resolve_punctuation_dup_players")
Candidate = resolver.Candidate
Group = resolver.Group


def _candidate(
    player_id: int,
    name: str,
    *,
    draft_year: int | None = None,
    is_stub: bool = False,
    external_ids: set[tuple[str, str]] | None = None,
    blocking_rows: int = 0,
    movable_rows: int = 0,
):  # returns resolver.Candidate; loaded at runtime, so not annotatable
    return Candidate(
        player_id=player_id,
        display_name=name,
        draft_year=draft_year,
        is_stub=is_stub,
        external_ids=external_ids or set(),
        blocking_rows=blocking_rows,
        movable_rows=movable_rows,
    )


def _classify(*members):  # members are resolver.Candidate instances
    return resolver.classify(Group(key="k", members=list(members)))


class TestRefusesToMergeDifferentPeople:
    """The failure mode that matters. Every case here is real."""

    def test_father_and_son_are_not_merged(self):
        """`ronharperjr`: Ron Harper (1986 draft) and Ron Harper Jr. are two players.

        Their display names are identical after punctuation stripping; only the
        conflicting Basketball-Reference ids distinguish them.
        """
        group = _classify(
            _candidate(
                1465,
                "Ron Harper Jr.",
                draft_year=1986,
                external_ids={("bbr", "harpero01")},
            ),
            _candidate(3089, "Ron Harper Jr.", external_ids={("bbr", "harpero02")}),
        )
        assert group.verdict == resolver.DIFFERENT
        assert "harpero01" in group.reason and "harpero02" in group.reason
        assert group.discard_id is None, "must not nominate anything for deletion"

    def test_second_father_son_pair_is_not_merged(self):
        """`scottypippenjr`: Scottie Pippen (1987 draft) vs Scotty Pippen Jr."""
        group = _classify(
            _candidate(
                1490,
                "Scotty Pippen Jr.",
                draft_year=1987,
                external_ids={("bbr", "pippesc01")},
            ),
            _candidate(4341, "Scotty Pippen Jr.", external_ids={("bbr", "pippesc02")}),
        )
        assert group.verdict == resolver.DIFFERENT

    def test_two_identified_players_are_not_merged(self):
        """Both rows carrying external ids means both are real, identified records."""
        group = _classify(
            _candidate(
                1402,
                "AJ Lawson",
                external_ids={("bbr", "lawsoaj01"), ("nba_stats", "1")},
            ),
            _candidate(
                1733,
                "A.J. Lawson",
                external_ids={("instagram", "ajlawson")},
                movable_rows=263,
            ),
        )
        assert group.verdict == resolver.DIFFERENT
        assert group.discard_id is None

    def test_disagreeing_draft_years_are_referred_not_merged(self):
        """`derrionreid`: same name, draft_year 2025 vs 2026 — unresolvable from here."""
        group = _classify(
            _candidate(6509, "Derrion Reid", draft_year=2025, is_stub=True),
            _candidate(6513, "Derrion Reid", draft_year=2026, is_stub=True),
        )
        assert group.verdict == resolver.REVIEW
        assert "draft_year" in group.reason

    def test_three_way_group_is_referred(self):
        """A father/son pair plus a stub cannot be resolved by rule — a human picks."""
        group = _classify(
            _candidate(
                1465,
                "Ron Harper Jr.",
                draft_year=1986,
                external_ids={("bbr", "harpero01")},
            ),
            _candidate(3089, "Ron Harper Jr.", external_ids={("bbr", "harpero02")}),
            _candidate(5742, "Ron Harper Jr", draft_year=2022, is_stub=True),
        )
        assert group.verdict == resolver.REVIEW
        assert group.discard_id is None


class TestAcceptsProvableDuplicates:
    """The safe pattern: an anonymous stub beside one identified player."""

    def test_stub_beside_identified_player_is_merged_into_it(self):
        """`rjbarrett`: punctuation-variant stub with no ids merges into the real row."""
        group = _classify(
            _candidate(
                1258,
                "RJ Barrett",
                draft_year=2019,
                external_ids={("bbr", "barrerj01")},
                movable_rows=83,
            ),
            _candidate(
                6294, "R.J. Barrett", draft_year=2019, is_stub=True, movable_rows=5
            ),
        )
        assert group.verdict == resolver.SAFE
        assert (group.keep_id, group.discard_id) == (1258, 6294)

    def test_direction_follows_the_data_not_the_id(self):
        """Production inverts the usual ordering, so ordering must not be assumed.

        `AJ Lawson` (id 1402) is the empty row while `A.J. Lawson` (id 1733) holds the
        data — a lower id is not reliably the survivor.
        """
        group = _classify(
            _candidate(9999, "Jaime Jaquez Jr", is_stub=True, movable_rows=4),
            _candidate(
                1546,
                "Jaime Jaquez Jr.",
                external_ids={("bbr", "jaquejai01")},
                movable_rows=12,
            ),
        )
        assert group.verdict == resolver.SAFE
        assert (group.keep_id, group.discard_id) == (1546, 9999)

    def test_movable_rows_on_the_stub_do_not_block(self):
        """Stub rows in reassignable tables are fine — moving them is the merge's job."""
        group = _classify(
            _candidate(
                1655,
                "Ja'Kobe Walter",
                draft_year=2024,
                external_ids={("bbr", "walteja01")},
                movable_rows=124,
            ),
            _candidate(
                6157, "JaKobe Walter", draft_year=2024, is_stub=True, movable_rows=6
            ),
        )
        assert group.verdict == resolver.SAFE


class TestRefusesWhatTheMergeCannotDo:
    """Do not nominate a merge that would fail partway through."""

    def test_stub_holding_unreassignable_rows_is_referred(self):
        """A discard with Summer League rows hits a RESTRICT FK mid-merge.

        Those 13 FK edges are unclassified in player_merge_service (see
        tests/unit/test_player_merge_fk_coverage.py), so the merge would fail rather
        than move them.
        """
        group = _classify(
            _candidate(
                1424,
                "Day'Ron Sharpe",
                external_ids={("bbr", "sharpda01")},
                movable_rows=10,
            ),
            _candidate(5901, "Day'Ron Sharpe", is_stub=True, blocking_rows=27),
        )
        assert group.verdict == resolver.REVIEW
        assert "RESTRICT" in group.reason
        assert group.discard_id is None

    def test_two_empty_stubs_are_referred_not_guessed(self):
        """Probably duplicates, but nothing identifies either — so a human decides."""
        group = _classify(
            _candidate(6510, "Trey Kaufmann Renn", draft_year=2026, is_stub=True),
            _candidate(6511, "Trey Kaufmann-Renn", draft_year=2026, is_stub=True),
        )
        assert group.verdict == resolver.REVIEW
        assert group.discard_id is None


@pytest.mark.parametrize(
    "verdict", [resolver.DIFFERENT, resolver.REVIEW], ids=["different-people", "review"]
)
def test_non_safe_verdicts_never_nominate_a_discard(verdict):
    """Only a SAFE verdict may carry a discard id — the executor merges on that alone."""
    groups = [
        _classify(
            _candidate(1, "A", external_ids={("bbr", "x1")}),
            _candidate(2, "A", external_ids={("bbr", "x2")}),
        ),
        _classify(
            _candidate(3, "B", draft_year=2025, is_stub=True),
            _candidate(4, "B", draft_year=2026, is_stub=True),
        ),
    ]
    for group in groups:
        if group.verdict == verdict:
            assert group.discard_id is None and group.keep_id is None
