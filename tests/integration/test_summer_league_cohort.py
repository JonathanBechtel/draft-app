"""Integration tests for the shared Summer League rostered-cohort selector."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueParticipation,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.services.summer_league.cohort import summer_league_cohort
from tests.integration.conftest import make_player

_N = {"i": 0}


async def _seed_competition(
    db: AsyncSession, *, year: int, league_id: str, venue_slug: str
) -> tuple[SummerLeagueEdition, SummerLeagueTeamEntry]:
    """Seed one competition with a single team entry."""
    _N["i"] += 1
    comp = SummerLeagueEdition(
        year=year,
        league_id=league_id,
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 10),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    team = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id=f"team-{_N['i']}",
        raw_team_name="Test Team",
        raw_team_abbreviation="TST",
        team_slug=f"tst-{_N['i']}",
    )
    db.add(team)
    await db.flush()
    assert team.id is not None
    return comp, team


async def _participate(
    db: AsyncSession,
    *,
    comp_id: int,
    team_entry_id: int,
    name: str,
    person_id: str,
    canonical_player_id: int | None,
) -> SummerLeagueParticipation:
    """Seed one participation row, resolved or unresolved."""
    sp = SummerLeagueSourcePlayer(
        nba_stats_person_id=person_id,
        raw_player_name=name,
        normalized_name=name.lower(),
        canonical_player_id=canonical_player_id,
    )
    db.add(sp)
    await db.flush()
    assert sp.id is not None
    part = SummerLeagueParticipation(
        competition_id=comp_id,
        team_entry_id=team_entry_id,
        source_player_id=sp.id,
        player_id=canonical_player_id,
        stint_no=1,
    )
    db.add(part)
    await db.flush()
    return part


@pytest.mark.asyncio
async def test_cohort_filters_by_year_league_and_excludes_out_of_scope(
    db_session: AsyncSession,
) -> None:
    """Cohort includes only in-scope resolved player_ids + all source_player_ids.

    Seeds two competitions: an in-scope one (California Classic, 2025) with a
    resolved and an unresolved participant, and an out-of-scope one (Salt Lake
    City, 2025) with a resolved participant that must not leak into the
    in-scope result.
    """
    resolved = make_player("Cohort", "Resolved", school="Duke")
    db_session.add(resolved)
    await db_session.flush()
    assert resolved.id is not None

    other = make_player("Cohort", "Other", school="Kansas")
    db_session.add(other)
    await db_session.flush()
    assert other.id is not None

    comp_a, team_a = await _seed_competition(
        db_session, year=2025, league_id="13", venue_slug="california_classic"
    )
    comp_b, team_b = await _seed_competition(
        db_session, year=2025, league_id="16", venue_slug="salt_lake_city"
    )
    assert comp_a.id is not None
    assert team_a.id is not None
    assert comp_b.id is not None
    assert team_b.id is not None

    resolved_part = await _participate(
        db_session,
        comp_id=comp_a.id,
        team_entry_id=team_a.id,
        name="Cohort Resolved",
        person_id="cohort-1",
        canonical_player_id=resolved.id,
    )
    unresolved_part = await _participate(
        db_session,
        comp_id=comp_a.id,
        team_entry_id=team_a.id,
        name="Cohort Unresolved",
        person_id="cohort-2",
        canonical_player_id=None,
    )
    out_of_scope_part = await _participate(
        db_session,
        comp_id=comp_b.id,
        team_entry_id=team_b.id,
        name="Cohort Other",
        person_id="cohort-3",
        canonical_player_id=other.id,
    )
    await db_session.commit()

    result = await summer_league_cohort(db_session, year=2025, league_id="13")

    assert result.player_ids == {resolved.id}
    assert result.source_player_ids == {
        resolved_part.source_player_id,
        unresolved_part.source_player_id,
    }
    assert other.id not in result.player_ids
    assert out_of_scope_part.source_player_id not in result.source_player_ids


@pytest.mark.asyncio
async def test_cohort_filters_by_venue_slug(db_session: AsyncSession) -> None:
    """Venue-slug filter scopes the cohort to a single competition."""
    player_x = make_player("Venue", "PlayerX", school="UNC")
    player_y = make_player("Venue", "PlayerY", school="UCLA")
    db_session.add(player_x)
    db_session.add(player_y)
    await db_session.flush()
    assert player_x.id is not None
    assert player_y.id is not None

    comp_x, team_x = await _seed_competition(
        db_session, year=2025, league_id="14", venue_slug="orlando"
    )
    comp_y, team_y = await _seed_competition(
        db_session, year=2025, league_id="15", venue_slug="las_vegas"
    )
    assert comp_x.id is not None
    assert team_x.id is not None
    assert comp_y.id is not None
    assert team_y.id is not None
    await _participate(
        db_session,
        comp_id=comp_x.id,
        team_entry_id=team_x.id,
        name="Venue PlayerX",
        person_id="venue-1",
        canonical_player_id=player_x.id,
    )
    await _participate(
        db_session,
        comp_id=comp_y.id,
        team_entry_id=team_y.id,
        name="Venue PlayerY",
        person_id="venue-2",
        canonical_player_id=player_y.id,
    )
    await db_session.commit()

    result = await summer_league_cohort(db_session, venue_slug="orlando")

    assert result.player_ids == {player_x.id}


@pytest.mark.asyncio
async def test_cohort_empty_scope_returns_empty_result(
    db_session: AsyncSession,
) -> None:
    """A scope with no participations returns empty sets, not an error."""
    result = await summer_league_cohort(db_session, year=1999, league_id="99")

    assert result.player_ids == set()
    assert result.source_player_ids == set()
