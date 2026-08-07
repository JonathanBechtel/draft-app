"""Unit coverage for the franchise -> team_program resolver (backbone, #796).

No DB round-trip: ``build_franchise_team_program_map`` is pure, and
``resolve_team_targets`` is exercised by monkeypatching its three private
DB-lookup helpers directly (the same pattern
``tests/unit/test_populate_org_model_cli.py`` uses for T3's population
script), rather than faking a full ``AsyncSession``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.backbone import team_program_resolution as resolution
from app.services.backbone.team_program_resolution import (
    NBA_STATS_TEAM_ID_TO_ABBREVIATION,
    AmbiguousTeamProgramError,
    TeamProgramRow,
    build_franchise_team_program_map,
    derive_org_slug,
    resolve_team_targets,
)
from scripts.populate_org_model_from_nba_teams import (
    derive_org_slug as scripts_derive_org_slug,
)


# ---------------------------------------------------------------------------
# derive_org_slug
# ---------------------------------------------------------------------------


def test_derive_org_slug_is_prefixed_with_the_nba_namespace() -> None:
    """The org slug is derived from the immutable nba_teams.slug, not the name."""
    assert derive_org_slug("lakers") == "nba-lakers"


@pytest.mark.parametrize(
    "nba_team_slug",
    ["lakers", "celtics", "trail-blazers", "76ers"],
)
def test_derive_org_slug_matches_the_scripts_re_export_byte_for_byte(
    nba_team_slug: str,
) -> None:
    """scripts/populate_org_model_from_nba_teams.py must re-export, not re-derive.

    519 of 622 dev ``summer_league_team_entries`` rows and every existing
    ``organizations``/``team_programs`` row are keyed on this exact string;
    a silently divergent implementation would re-key them.
    """
    assert scripts_derive_org_slug(nba_team_slug) == derive_org_slug(nba_team_slug)


def test_scripts_module_re_exports_the_same_function_object() -> None:
    """The scripts module must import, not redefine, the backbone function."""
    assert scripts_derive_org_slug is derive_org_slug


# ---------------------------------------------------------------------------
# NBA_STATS_TEAM_ID_TO_ABBREVIATION
# ---------------------------------------------------------------------------


def test_nba_stats_team_id_map_covers_all_thirty_franchises() -> None:
    """Every current NBA franchise is represented exactly once."""
    assert len(NBA_STATS_TEAM_ID_TO_ABBREVIATION) == 30
    assert len(set(NBA_STATS_TEAM_ID_TO_ABBREVIATION.values())) == 30


# ---------------------------------------------------------------------------
# build_franchise_team_program_map (the ambiguity guard)
# ---------------------------------------------------------------------------


def test_build_franchise_team_program_map_returns_empty_for_no_rows() -> None:
    """An empty org set (T3 hasn't populated anything) returns an empty map."""
    assert build_franchise_team_program_map([]) == {}


def test_build_franchise_team_program_map_resolves_a_single_program_org() -> None:
    """One organization owning exactly one team_program resolves cleanly."""
    rows = [
        TeamProgramRow(
            team_program_id=42, team_program_slug="nba-lakers", organization_id=7
        )
    ]

    result = build_franchise_team_program_map(rows)

    assert result == {"nba-lakers": 42}


def test_build_franchise_team_program_map_raises_on_a_two_program_org() -> None:
    """An organization owning two team_programs is ambiguous and must raise.

    This is the exact bug the old scripts/backfill_* dict comprehensions had:
    keying on organization_id let the second row silently win. The promoted
    resolver refuses to guess.
    """
    rows = [
        TeamProgramRow(
            team_program_id=1, team_program_slug="nba-lakers-senior", organization_id=7
        ),
        TeamProgramRow(
            team_program_id=2, team_program_slug="nba-lakers-g-league", organization_id=7
        ),
    ]

    with pytest.raises(AmbiguousTeamProgramError):
        build_franchise_team_program_map(rows)


def test_build_franchise_team_program_map_keeps_unrelated_orgs_independent() -> None:
    """Two distinct single-program organizations both resolve independently.

    The map key is ``team_programs.slug``, not ``organization_id``, so
    neither entry can collide with the other.
    """
    rows = [
        TeamProgramRow(
            team_program_id=1, team_program_slug="nba-lakers", organization_id=7
        ),
        TeamProgramRow(
            team_program_id=2, team_program_slug="nba-celtics", organization_id=8
        ),
    ]

    result = build_franchise_team_program_map(rows)

    assert result == {"nba-lakers": 1, "nba-celtics": 2}


# ---------------------------------------------------------------------------
# resolve_team_targets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_team_targets_returns_none_none_for_a_non_franchise_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-NBA/select-squad provider id is a correct NULL, never a guess.

    No DB lookup helper is even called -- the static id map short-circuits
    before any query, which is also why this never fails for an unseeded
    test database.
    """

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("no DB lookup should run for an unknown provider id")

    monkeypatch.setattr(resolution, "_find_nba_team_by_abbreviation", fail)

    result = await resolve_team_targets(object(), nba_stats_team_id="9999999999")

    assert result == (None, None)


@pytest.mark.asyncio
async def test_resolve_team_targets_returns_none_none_for_a_missing_team_id() -> None:
    """A ``None``/empty provider id resolves to NULL on both targets."""
    assert await resolve_team_targets(object(), nba_stats_team_id=None) == (None, None)
    assert await resolve_team_targets(object(), nba_stats_team_id="") == (None, None)


class _FakeNbaTeam:
    def __init__(self, *, id: int, slug: str) -> None:
        self.id = id
        self.slug = slug


@pytest.mark.asyncio
async def test_resolve_team_targets_hit_resolves_both_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known franchise with a populated org model resolves both targets."""

    async def fake_find_nba_team(_db: Any, abbreviation: str) -> _FakeNbaTeam | None:
        assert abbreviation == "LAL"
        return _FakeNbaTeam(id=5, slug="lakers")

    async def fake_find_organization_id(_db: Any, org_slug: str) -> int | None:
        assert org_slug == "nba-lakers"
        return 70

    async def fake_find_team_program_rows(
        _db: Any, organization_id: int
    ) -> list[TeamProgramRow]:
        assert organization_id == 70
        return [
            TeamProgramRow(
                team_program_id=900,
                team_program_slug="nba-lakers",
                organization_id=70,
            )
        ]

    monkeypatch.setattr(
        resolution, "_find_nba_team_by_abbreviation", fake_find_nba_team
    )
    monkeypatch.setattr(resolution, "_find_organization_id", fake_find_organization_id)
    monkeypatch.setattr(
        resolution, "_find_team_program_rows", fake_find_team_program_rows
    )

    result = await resolve_team_targets(object(), nba_stats_team_id="1610612747")

    assert result == (5, 900)


@pytest.mark.asyncio
async def test_resolve_team_targets_miss_when_nba_teams_row_is_unseeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recognized franchise id with no matching nba_teams row is NULL on both."""

    async def fake_find_nba_team(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        resolution, "_find_nba_team_by_abbreviation", fake_find_nba_team
    )

    result = await resolve_team_targets(object(), nba_stats_team_id="1610612747")

    assert result == (None, None)


@pytest.mark.asyncio
async def test_resolve_team_targets_leaves_team_program_null_when_t3_has_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved franchise with no organization yet fills nba_team_id only."""

    async def fake_find_nba_team(*_args: Any, **_kwargs: Any) -> _FakeNbaTeam:
        return _FakeNbaTeam(id=5, slug="lakers")

    async def fake_find_organization_id(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        resolution, "_find_nba_team_by_abbreviation", fake_find_nba_team
    )
    monkeypatch.setattr(resolution, "_find_organization_id", fake_find_organization_id)

    result = await resolve_team_targets(object(), nba_stats_team_id="1610612747")

    assert result == (5, None)


@pytest.mark.asyncio
async def test_resolve_team_targets_ambiguous_organization_leaves_program_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An organization owning two programs resolves nba_team_id but not the program.

    Per the entity-resolution rule (ambiguous or unknown -> NULL, never a
    guess), a per-row ingest call does not abort over one bad organization --
    it degrades to the same NULL an unresolved team already gets.
    """

    async def fake_find_nba_team(*_args: Any, **_kwargs: Any) -> _FakeNbaTeam:
        return _FakeNbaTeam(id=5, slug="lakers")

    async def fake_find_organization_id(*_args: Any, **_kwargs: Any) -> int:
        return 70

    async def fake_find_team_program_rows(
        *_args: Any, **_kwargs: Any
    ) -> list[TeamProgramRow]:
        return [
            TeamProgramRow(
                team_program_id=1, team_program_slug="nba-lakers-a", organization_id=70
            ),
            TeamProgramRow(
                team_program_id=2, team_program_slug="nba-lakers-b", organization_id=70
            ),
        ]

    monkeypatch.setattr(
        resolution, "_find_nba_team_by_abbreviation", fake_find_nba_team
    )
    monkeypatch.setattr(resolution, "_find_organization_id", fake_find_organization_id)
    monkeypatch.setattr(
        resolution, "_find_team_program_rows", fake_find_team_program_rows
    )

    result = await resolve_team_targets(object(), nba_stats_team_id="1610612747")

    assert result == (5, None)
