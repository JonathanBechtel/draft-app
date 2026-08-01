"""Integration tests for SL-cohort bio-enrichment target selection."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_bio_snapshots import PlayerBioSnapshot
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.player_status import PlayerStatus
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueParticipation,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.services.summer_league.bio_enrichment_targets import (
    select_bio_enrichment_targets,
)
from tests.integration.conftest import make_player

_N = {"i": 0}


async def _seed_competition(
    db: AsyncSession, *, year: int, league_id: str, venue_slug: str
) -> tuple[SummerLeagueCompetition, SummerLeagueTeamEntry]:
    """Seed one competition with a single team entry."""
    _N["i"] += 1
    comp = SummerLeagueCompetition(
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
async def test_bio_targets_restrict_to_bbref_having_cohort_players(
    db_session: AsyncSession,
) -> None:
    """Only cohort players with a bbref external id become scrape/ingest targets.

    Seeds a 2025 California Classic cohort with two resolved players: one has
    a `bbref` external id (should appear in the target slug set) and one does
    not (should land in the manual-review list). A resolved player in an
    out-of-scope competition (Salt Lake City) must not leak into either set.
    """
    has_bbref = make_player("Has", "Bbref", school="Duke")
    no_bbref = make_player("No", "Bbref", school="Kansas")
    out_of_scope = make_player("Out", "OfScope", school="UNC")
    db_session.add_all([has_bbref, no_bbref, out_of_scope])
    await db_session.flush()
    assert has_bbref.id is not None
    assert no_bbref.id is not None
    assert out_of_scope.id is not None

    db_session.add(
        PlayerExternalId(
            player_id=has_bbref.id,
            system="bbr",
            external_id="bbrefha01",
            source_url="https://www.basketball-reference.com/players/b/bbrefha01.html",
        )
    )
    await db_session.flush()

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

    await _participate(
        db_session,
        comp_id=comp_a.id,
        team_entry_id=team_a.id,
        name="Has Bbref",
        person_id="bio-1",
        canonical_player_id=has_bbref.id,
    )
    await _participate(
        db_session,
        comp_id=comp_a.id,
        team_entry_id=team_a.id,
        name="No Bbref",
        person_id="bio-2",
        canonical_player_id=no_bbref.id,
    )
    await _participate(
        db_session,
        comp_id=comp_b.id,
        team_entry_id=team_b.id,
        name="Out OfScope",
        person_id="bio-3",
        canonical_player_id=out_of_scope.id,
    )
    await db_session.commit()

    targets = await select_bio_enrichment_targets(db_session, year=2025, league_id="13")

    assert targets.slugs == {"bbrefha01"}
    assert targets.player_id_by_slug == {"bbrefha01": has_bbref.id}
    assert targets.manual_review_player_ids == {no_bbref.id}
    assert out_of_scope.id not in targets.manual_review_player_ids
    assert out_of_scope.id not in targets.player_id_by_slug.values()


@pytest.mark.asyncio
async def test_bio_targets_empty_cohort_returns_empty_result(
    db_session: AsyncSession,
) -> None:
    """A scope with no participations returns empty sets, not an error."""
    targets = await select_bio_enrichment_targets(db_session, year=1999, league_id="99")

    assert targets.slugs == set()
    assert targets.player_id_by_slug == {}
    assert targets.manual_review_player_ids == set()


@pytest.mark.asyncio
async def test_bio_targets_all_cohort_players_have_bbref_ids(
    db_session: AsyncSession,
) -> None:
    """When every cohort player has a bbref id, the manual-review list is empty."""
    player_one = make_player("All", "Matched", school="Gonzaga")
    db_session.add(player_one)
    await db_session.flush()
    assert player_one.id is not None

    db_session.add(
        PlayerExternalId(
            player_id=player_one.id,
            system="bbr",
            external_id="allmat01",
            source_url="https://www.basketball-reference.com/players/a/allmat01.html",
        )
    )
    await db_session.flush()

    comp, team = await _seed_competition(
        db_session, year=2025, league_id="14", venue_slug="orlando"
    )
    assert comp.id is not None
    assert team.id is not None
    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="All Matched",
        person_id="bio-4",
        canonical_player_id=player_one.id,
    )
    await db_session.commit()

    targets = await select_bio_enrichment_targets(db_session, venue_slug="orlando")

    assert targets.slugs == {"allmat01"}
    assert targets.manual_review_player_ids == set()


@pytest.mark.asyncio
async def test_bio_targets_force_changes_and_retry_failed_enrichment(
    db_session: AsyncSession,
) -> None:
    """Changed players are forced while prior BBR failures remain retryable."""
    changed = make_player("Changed", "Player", school="Duke")
    already_enriched = make_player("Already", "Enriched", school="Kansas")
    failed = make_player("Failed", "Player", school="UCLA")
    db_session.add_all([changed, already_enriched, failed])
    await db_session.flush()
    assert changed.id is not None
    assert already_enriched.id is not None
    assert failed.id is not None

    db_session.add_all(
        [
            PlayerExternalId(
                player_id=changed.id,
                system="bbr",
                external_id="changed01",
            ),
            PlayerExternalId(
                player_id=already_enriched.id,
                system="bbr",
                external_id="already01",
            ),
            PlayerExternalId(
                player_id=failed.id,
                system="bbr",
                external_id="failed01",
            ),
            PlayerStatus(player_id=changed.id, source="bbr"),
            PlayerStatus(player_id=already_enriched.id, source="bbr"),
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
        name="Changed Player",
        person_id="bio-5",
        canonical_player_id=changed.id,
    )
    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Already Enriched",
        person_id="bio-6",
        canonical_player_id=already_enriched.id,
    )
    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Failed Player",
        person_id="bio-7",
        canonical_player_id=failed.id,
    )
    await db_session.commit()

    targets = await select_bio_enrichment_targets(
        db_session,
        year=2025,
        league_id="13",
        player_ids={changed.id},
        retry_unenriched=True,
    )

    assert targets.slugs == {"changed01", "failed01"}
    assert "already01" not in targets.slugs


@pytest.mark.asyncio
async def test_bio_targets_retry_ignores_snapshot_from_a_non_bbr_source(
    db_session: AsyncSession,
) -> None:
    """A ``PlayerBioSnapshot`` from a different source does not count as a BBR success.

    #719 item 7: the "successfully enriched" check used to count any
    ``PlayerBioSnapshot`` row for the player, regardless of ``source``. The
    BBR ingest is the only writer today, so this was harmless in practice --
    but a second bio-source writer would have silently looked like a
    completed BBR enrichment and stopped this player from ever being
    retried. This player has a non-BBR snapshot and no ``PlayerStatus`` BBR
    success row, so it must still surface as a retry target.

    Both players are deliberately left out of ``player_ids`` (the forced-change
    set). That set unions straight into the targets, so passing either player
    there would force-include it regardless of the ``source`` filter and make
    the assertion vacuous. Routing inclusion solely through
    ``cohort.player_ids - successfully_enriched`` is what makes deleting the
    ``PlayerBioSnapshot.source == SYSTEM_BBR`` filter turn this test red. The
    BBR-snapshot player is the negative control: it proves the filter still
    excludes a genuine BBR success rather than having been dropped entirely.
    """
    other_source_player = make_player("Other", "Source", school="Gonzaga")
    bbr_snapshot_player = make_player("Bbr", "Snapshot", school="Gonzaga")
    db_session.add_all([other_source_player, bbr_snapshot_player])
    await db_session.flush()
    assert other_source_player.id is not None
    assert bbr_snapshot_player.id is not None

    db_session.add_all(
        [
            PlayerExternalId(
                player_id=other_source_player.id,
                system="bbr",
                external_id="othersrc1",
            ),
            PlayerBioSnapshot(
                player_id=other_source_player.id,
                source="some_other_source",
            ),
            PlayerExternalId(
                player_id=bbr_snapshot_player.id,
                system="bbr",
                external_id="bbrsnap01",
            ),
            PlayerBioSnapshot(
                player_id=bbr_snapshot_player.id,
                source="bbr",
            ),
        ]
    )
    await db_session.flush()

    comp, team = await _seed_competition(
        db_session, year=2025, league_id="16", venue_slug="utah"
    )
    assert comp.id is not None
    assert team.id is not None
    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Other Source",
        person_id="bio-8",
        canonical_player_id=other_source_player.id,
    )
    await _participate(
        db_session,
        comp_id=comp.id,
        team_entry_id=team.id,
        name="Bbr Snapshot",
        person_id="bio-9",
        canonical_player_id=bbr_snapshot_player.id,
    )
    await db_session.commit()

    targets = await select_bio_enrichment_targets(
        db_session,
        year=2025,
        league_id="16",
        player_ids=set(),
        retry_unenriched=True,
    )

    assert targets.slugs == {"othersrc1"}
    assert "bbrsnap01" not in targets.slugs
