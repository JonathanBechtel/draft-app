"""Integration tests for SL-cohort targeting in the college-stats sweep.

Seeds Summer League cohort players with and without a ``school`` + BBRef
external id, then asserts that ``run_college_stats_sweep(sl_cohort=True)``
restricts its target set to the eligible cohort players and enumerates the
rest (non-NCAA/international) as no-source rather than failing them.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.player_college_stats import PlayerCollegeStats
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueParticipation,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.services import college_stats_service
from app.services.college_stats_service import run_college_stats_sweep
from tests.integration.conftest import make_player

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_network_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out live BBRef HTTP fetches for every test in this module.

    ``run_college_stats_sweep`` always calls ``fetch_player_html`` (even in
    ``dry_run`` mode) before deciding whether to scrape. These tests only
    exercise the SL-cohort *target selection* (which is computed up front,
    before the fetch), so we short-circuit the network call entirely.
    """
    monkeypatch.setattr(
        college_stats_service, "fetch_player_html", lambda *a, **k: None
    )


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


async def _participate(  # noqa: PLR0913
    db: AsyncSession,
    *,
    comp_id: int,
    team_entry_id: int,
    name: str,
    person_id: str,
    canonical_player_id: int,
) -> SummerLeagueParticipation:
    """Seed one resolved participation row."""
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


class _FakeSessionCtx:
    """Async context manager that always yields the given test session."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def __aenter__(self) -> AsyncSession:
        return self._db

    async def __aexit__(self, *exc: object) -> None:
        return None


def _session_factory_for(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Build a session factory that always returns the given test session.

    ``run_college_stats_sweep`` takes a session factory (not a session) so it
    can open short-lived transactions per player; the integration fixtures
    give us a single shared ``db_session`` instead, so we wrap it.
    """
    return lambda: _FakeSessionCtx(db)  # type: ignore[return-value]


async def test_sl_cohort_restricts_target_and_enumerates_no_source(
    db_session: AsyncSession,
) -> None:
    """Cohort selection targets only school+bbref players; rest are no-source.

    Seeds three SL cohort players in the same competition: one NCAA player
    with a resolved BBRef id (eligible), one international player with no
    ``school`` (no-source), and one NCAA player with a school but no BBRef
    external id (no-source). Asserts the sweep only attempts the eligible
    player and lists the other two by name in ``result.no_source``.
    """
    eligible = make_player("Cohort", "Eligible", school="Duke")
    international = make_player("Cohort", "International", school=None)
    no_bbref = make_player("Cohort", "NoBbref", school="Kansas")
    db_session.add_all([eligible, international, no_bbref])
    await db_session.flush()
    assert eligible.id is not None
    assert international.id is not None
    assert no_bbref.id is not None

    db_session.add(
        PlayerExternalId(
            player_id=eligible.id,
            system="bbr",
            external_id="eligco01",
            source_url="https://www.basketball-reference.com/players/e/eligco01.html",
        )
    )
    await db_session.flush()

    comp, team = await _seed_competition(
        db_session, year=2025, league_id="13", venue_slug="california_classic"
    )
    assert comp.id is not None
    assert team.id is not None

    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Cohort Eligible",
        person_id="cec-1",
        canonical_player_id=eligible.id,
    )
    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Cohort International",
        person_id="cec-2",
        canonical_player_id=international.id,
    )
    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Cohort NoBbref",
        person_id="cec-3",
        canonical_player_id=no_bbref.id,
    )
    await db_session.commit()

    session_factory = _session_factory_for(db_session)

    result = await run_college_stats_sweep(
        session_factory,
        dry_run=True,
        sl_cohort=True,
        sl_year=2025,
        sl_league_id="13",
    )

    assert result.players_attempted == 1
    assert result.players_failed == 0
    assert len(result.no_source) == 2
    no_source_text = "\n".join(result.no_source)
    assert "Cohort International" in no_source_text
    assert "Cohort NoBbref" in no_source_text
    assert "Cohort Eligible" not in no_source_text


async def test_sl_cohort_filters_out_of_scope_competition(
    db_session: AsyncSession,
) -> None:
    """A year/league filter excludes cohort players outside that scope.

    Seeds an eligible player in an out-of-scope competition (different year)
    and asserts the sweep finds nothing to attempt and reports no no-source
    entries either, since the player is outside the requested cohort scope.
    """
    outside = make_player("Cohort", "OutOfScope", school="UCLA")
    db_session.add(outside)
    await db_session.flush()
    assert outside.id is not None

    db_session.add(
        PlayerExternalId(
            player_id=outside.id,
            system="bbr",
            external_id="outsco01",
            source_url="https://www.basketball-reference.com/players/o/outsco01.html",
        )
    )
    await db_session.flush()

    comp, team = await _seed_competition(
        db_session, year=2019, league_id="16", venue_slug="salt_lake_city"
    )
    assert comp.id is not None
    assert team.id is not None
    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Cohort OutOfScope",
        person_id="cec-4",
        canonical_player_id=outside.id,
    )
    await db_session.commit()

    session_factory = _session_factory_for(db_session)

    result = await run_college_stats_sweep(
        session_factory,
        dry_run=True,
        sl_cohort=True,
        sl_year=2025,
        sl_league_id="13",
    )

    assert result.players_attempted == 0
    assert result.no_source == []


async def test_sl_cohort_all_no_source_finds_no_eligible_players(
    db_session: AsyncSession,
) -> None:
    """When every cohort player is ineligible, no-source is populated with 0 attempted.

    Seeds a single international cohort player (no ``school``), so the
    eligible target set is empty but the no-source enumeration still runs
    and reports that player, exercising the "no players found, but no-source
    is non-empty" branch.
    """
    international = make_player("Cohort", "OnlyIntl", school=None)
    db_session.add(international)
    await db_session.flush()
    assert international.id is not None

    comp, team = await _seed_competition(
        db_session, year=2025, league_id="14", venue_slug="orlando"
    )
    assert comp.id is not None
    assert team.id is not None
    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Cohort OnlyIntl",
        person_id="cec-5",
        canonical_player_id=international.id,
    )
    await db_session.commit()

    session_factory = _session_factory_for(db_session)

    result = await run_college_stats_sweep(
        session_factory,
        dry_run=True,
        sl_cohort=True,
        sl_year=2025,
        sl_league_id="14",
    )

    assert result.players_attempted == 0
    assert len(result.no_source) == 1
    assert "Cohort OnlyIntl" in result.no_source[0]


async def test_without_sl_cohort_flag_behaves_as_full_sweep(
    db_session: AsyncSession,
) -> None:
    """With sl_cohort=False (default), all eligible players are targeted.

    Regression check that the new cohort machinery doesn't change existing
    non-cohort behavior: a player with school + bbref id outside any SL
    cohort is still picked up, and no no-source list is produced.
    """
    player = make_player("Plain", "Sweep", school="Villanova")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None

    db_session.add(
        PlayerExternalId(
            player_id=player.id,
            system="bbr",
            external_id="sweepl01",
            source_url="https://www.basketball-reference.com/players/s/sweepl01.html",
        )
    )
    await db_session.flush()
    await db_session.commit()

    session_factory = _session_factory_for(db_session)

    result = await run_college_stats_sweep(session_factory, dry_run=True)

    assert result.players_attempted == 1
    assert result.no_source == []


async def test_only_missing_does_not_report_enriched_players_as_no_source(
    db_session: AsyncSession,
) -> None:
    """--only-missing must not misclassify already-enriched players as no-source.

    An eligible cohort player (school + BBRef id) who already has
    ``sports_reference`` stats is filtered out of the *target* set by
    ``only_missing``, but is enriched — not no-source. Only the genuinely
    ineligible player (no school) belongs in ``result.no_source``. Regression
    guard: computing no-source from the only_missing-filtered target set would
    wrongly list the enriched player as "no school + BBRef id on record".
    """
    enriched = make_player("Cohort", "Enriched", school="Duke")
    international = make_player("Cohort", "Intl", school=None)
    db_session.add_all([enriched, international])
    await db_session.flush()
    assert enriched.id is not None
    assert international.id is not None

    db_session.add(
        PlayerExternalId(
            player_id=enriched.id,
            system="bbr",
            external_id="enrico01",
            source_url="https://www.basketball-reference.com/players/e/enrico01.html",
        )
    )
    # Existing sports_reference stats -> excluded by --only-missing.
    db_session.add(
        PlayerCollegeStats(
            player_id=enriched.id, season="2024-25", source="sports_reference"
        )
    )
    await db_session.flush()

    comp, team = await _seed_competition(
        db_session, year=2025, league_id="13", venue_slug="california_classic"
    )
    assert comp.id is not None
    assert team.id is not None

    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Cohort Enriched",
        person_id="cme-1",
        canonical_player_id=enriched.id,
    )
    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Cohort Intl",
        person_id="cme-2",
        canonical_player_id=international.id,
    )
    await db_session.commit()

    session_factory = _session_factory_for(db_session)

    result = await run_college_stats_sweep(
        session_factory,
        dry_run=True,
        only_missing=True,
        sl_cohort=True,
        sl_year=2025,
        sl_league_id="13",
    )

    # Enriched player is filtered from the target set by --only-missing...
    assert result.players_attempted == 0
    # ...but must NOT be reported as no-source; only the international is.
    no_source_text = "\n".join(result.no_source)
    assert "Cohort Intl" in no_source_text
    assert "Cohort Enriched" not in no_source_text


async def test_changed_player_is_forced_alongside_missing_stats_players(
    db_session: AsyncSession,
) -> None:
    """Roster changes refresh even an eligible player with prior stats."""
    forced = make_player("Cohort", "Forced", school="Duke")
    missing = make_player("Cohort", "Missing", school="Kansas")
    db_session.add_all([forced, missing])
    await db_session.flush()
    assert forced.id is not None
    assert missing.id is not None

    db_session.add_all(
        [
            PlayerExternalId(
                player_id=forced.id,
                system="bbr",
                external_id="forced01",
            ),
            PlayerExternalId(
                player_id=missing.id,
                system="bbr",
                external_id="missing01",
            ),
            PlayerCollegeStats(
                player_id=forced.id,
                season="2024-25",
                source="sports_reference",
            ),
        ]
    )
    await db_session.flush()

    comp, team = await _seed_competition(
        db_session, year=2025, league_id="13", venue_slug="california_classic"
    )
    assert comp.id is not None
    assert team.id is not None
    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Cohort Forced",
        person_id="ccf-1",
        canonical_player_id=forced.id,
    )
    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Cohort Missing",
        person_id="ccf-2",
        canonical_player_id=missing.id,
    )
    await db_session.commit()

    result = await run_college_stats_sweep(
        _session_factory_for(db_session),
        dry_run=True,
        only_missing=True,
        sl_cohort=True,
        sl_year=2025,
        sl_league_id="13",
        sl_player_ids={forced.id},
    )

    assert result.players_attempted == 2
    assert result.players_skipped == 2
