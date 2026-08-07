"""Unit coverage for the dual-read team-target resolution helper.

No DB round-trip: constructs bare ``PlayerAffiliation``/``SummerLeagueTeamEntry``
instances (never flushed) and asserts the resolution rule -- prefer
``team_program_id``, fall back to ``nba_team_id``, and return ``None`` when
neither target is known. The ``SummerLeagueTeamEntry`` cases (ticket #784)
exercise the exact same ``resolve_affiliation_target``/``resolve_team_target``
function T4 (#783) shipped for ``PlayerAffiliation`` -- proof the helper was
reused, not forked, for the second dual-read table.

The ``resolve_team_target_filter`` cases (ticket #795) cover the SQL projection
of the same rule at the query-builder level: which branches the clause emits
for each combination of targets. That the emitted SQL *selects* the same rows
the Python resolver does is proved against a real Postgres in
``tests/integration/test_summer_league_franchise.py``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects import postgresql

from app.schemas.player_affiliation import AffiliationType, PlayerAffiliation
from app.schemas.summer_league import SummerLeagueTeamEntry
from app.services.player_affiliation import (
    NbaTeamRef,
    TeamProgramRef,
    resolve_affiliation_target,
    resolve_team_target,
    resolve_team_target_filter,
)


def _affiliation(
    *, team_program_id: int | None, nba_team_id: int | None
) -> PlayerAffiliation:
    return PlayerAffiliation(
        player_id=1,
        team_program_id=team_program_id,
        nba_team_id=nba_team_id,
        affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
        source="test",
    )


def test_prefers_team_program_id_when_both_are_set() -> None:
    """A dual-populated row resolves through the generic org model."""
    affiliation = _affiliation(team_program_id=42, nba_team_id=7)

    result = resolve_affiliation_target(affiliation)

    assert result == TeamProgramRef(team_program_id=42)


def test_falls_back_to_nba_team_id_when_team_program_id_is_null() -> None:
    """A legacy SL row without a program yet still resolves via nba_team_id."""
    affiliation = _affiliation(team_program_id=None, nba_team_id=7)

    result = resolve_affiliation_target(affiliation)

    assert result == NbaTeamRef(nba_team_id=7)


def test_returns_none_when_both_targets_are_null() -> None:
    """An unresolved roster row (no franchise, no program) resolves to nothing."""
    affiliation = _affiliation(team_program_id=None, nba_team_id=None)

    result = resolve_affiliation_target(affiliation)

    assert result is None


def test_team_program_id_wins_even_when_nba_team_id_is_null() -> None:
    """A non-NBA-sourced affiliation resolves purely through the program."""
    affiliation = _affiliation(team_program_id=99, nba_team_id=None)

    result = resolve_affiliation_target(affiliation)

    assert result == TeamProgramRef(team_program_id=99)


def _team_entry(
    *, team_program_id: int | None, nba_team_id: int | None
) -> SummerLeagueTeamEntry:
    return SummerLeagueTeamEntry(
        competition_id=1,
        team_program_id=team_program_id,
        nba_team_id=nba_team_id,
        nba_stats_team_id="1610612737",
        raw_team_name="Test Team",
        team_slug="test-team",
    )


def test_resolve_team_target_prefers_team_program_id_for_a_team_entry() -> None:
    """The shared helper, called on a SummerLeagueTeamEntry, prefers the program."""
    entry = _team_entry(team_program_id=42, nba_team_id=7)

    result = resolve_team_target(entry)

    assert result == TeamProgramRef(team_program_id=42)


def test_resolve_team_target_falls_back_to_nba_team_id_for_a_team_entry() -> None:
    """A legacy team entry without a program yet still resolves via nba_team_id."""
    entry = _team_entry(team_program_id=None, nba_team_id=7)

    result = resolve_team_target(entry)

    assert result == NbaTeamRef(nba_team_id=7)


def test_resolve_team_target_returns_none_for_a_team_entry_with_neither_target() -> None:
    """An unresolved team entry (no franchise, no program) resolves to nothing."""
    entry = _team_entry(team_program_id=None, nba_team_id=None)

    result = resolve_team_target(entry)

    assert result is None


def test_resolve_affiliation_target_is_the_same_function_as_resolve_team_target() -> None:
    """The T4 name is a back-compat alias, not a second implementation."""
    assert resolve_affiliation_target is resolve_team_target


# --------------------------------------------------------------------------- #
# resolve_team_target_filter -- the SQL projection of the same rule (#795)
# --------------------------------------------------------------------------- #


def _compiled(clause: Any) -> str:
    """Render a clause to literal SQL so the shape can be asserted, not guessed."""
    return str(
        clause.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).replace("\n", " ")


def test_filter_accepts_both_a_program_target_and_a_legacy_target() -> None:
    """The clause is two branches: the program, or a program-less legacy row."""
    sql = _compiled(
        resolve_team_target_filter(
            SummerLeagueTeamEntry, team_program_id=42, nba_team_id=7
        )
    )

    assert "team_program_id = 42" in sql
    assert "team_program_id IS NULL" in sql
    assert "nba_team_id = 7" in sql
    assert " OR " in sql


def test_filter_never_matches_a_legacy_target_on_a_retargeted_row() -> None:
    """The ``IS NULL`` term is present, so an already-retargeted row is excluded.

    This is the clause a hand-written ``OR`` gets wrong: without the ``IS NULL``
    guard, an entry retargeted to a *different* program that still carries its
    legacy ``nba_team_id`` would match here as well as under its real program.
    """
    sql = _compiled(
        resolve_team_target_filter(
            SummerLeagueTeamEntry, team_program_id=42, nba_team_id=7
        )
    )

    # The nba_team_id branch is conjoined with the IS NULL guard, never bare.
    assert "team_program_id IS NULL AND" in sql


def test_filter_degenerates_to_the_legacy_column_when_no_program_is_known() -> None:
    """Pre-population, the clause is byte-for-byte the query this page always ran."""
    sql = _compiled(
        resolve_team_target_filter(
            SummerLeagueTeamEntry, team_program_id=None, nba_team_id=7
        )
    )

    assert "nba_team_id = 7" in sql
    # No program branch at all -- `IS NULL` must not become a match-everything
    # predicate that sweeps in every unpopulated row in the table.
    assert " OR " not in sql
    assert "team_program_id IS NULL AND" in sql


def test_filter_matches_only_the_program_when_there_is_no_franchise() -> None:
    """A non-NBA caller with no franchise identity filters on the program alone."""
    sql = _compiled(
        resolve_team_target_filter(
            SummerLeagueTeamEntry, team_program_id=42, nba_team_id=None
        )
    )

    assert "team_program_id = 42" in sql
    assert "nba_team_id" not in sql
    assert " OR " not in sql


def test_filter_with_no_targets_matches_nothing_rather_than_everything() -> None:
    """No target means an empty result, never an unfiltered table scan."""
    sql = _compiled(
        resolve_team_target_filter(
            SummerLeagueTeamEntry, team_program_id=None, nba_team_id=None
        )
    )

    assert sql == "false"
