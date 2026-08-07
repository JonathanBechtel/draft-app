"""Integration coverage for multi-squad franchise resolution (#810).

Four franchises field a second/third Summer League squad in the same
competition edition (Golden State Warriors Gold/Blue, Orlando Magic
White/Blue, Sacramento Kings 1/2, Utah Jazz White/Blue). These tests prove
the shape the ticket exists to protect: a ``17…``/``18…`` provider id
resolves to a *sibling* ``team_program_id``, never the franchise's primary
one, and two entries in the *same* competition edition for the *same*
franchise land on two distinct programs -- the case naive prefix-stripping
onto one program would have corrupted.

They also prove the ambiguity guard (``AmbiguousTeamProgramError``) does
*not* fire for a legitimate multi-squad franchise, on both of its remaining
callers: the ingest-time resolver's primary-program path
(``resolve_team_targets``) and the read-side franchise-page resolver
(``resolve_franchise_team_program_id``). ``scripts/backfill_sl_team_entry_team_program.py``
(strategy 1, ``franchise_nba_team_id_to_team_program_id``) is exercised the
same way in ``test_backfill_sl_team_entry_team_program.py``.

These tests run against the live integration-test Postgres schema (via
``db_session`` from conftest), so they require ``TEST_DATABASE_URL`` and
``PYTEST_ALLOW_DB=1``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.nba_teams import NbaTeam
from app.schemas.summer_league import SummerLeagueEdition, SummerLeagueTeamEntry
from app.services.backbone.team_program_resolution import (
    resolve_franchise_team_program_id,
    resolve_team_targets,
)
from scripts.backfill_sl_team_entry_team_program import run_backfill
from scripts.populate_multi_squad_team_programs import (
    run_population as run_multi_squad_population,
)
from scripts.populate_org_model_from_nba_teams import run_population

GSW_PRIMARY_ID = "1610612744"
GSW_GOLD_ID = "1710612744"
GSW_BLUE_ID = "1810612744"


async def _seed_warriors_org_model(db: AsyncSession) -> int:
    """Seed the Warriors franchise plus a decoy (Lakers), then run both population steps.

    Returns the Warriors' ``nba_teams.id``. The Lakers decoy means a resolver
    that grabs "any" organization's program cannot pass.
    """
    warriors = NbaTeam(name="Golden State Warriors", abbreviation="GSW", slug="warriors")
    db.add(warriors)
    db.add(NbaTeam(name="Los Angeles Lakers", abbreviation="LAL", slug="lakers"))
    await db.commit()
    warriors_id = warriors.id
    assert warriors_id is not None

    primary_report = await run_population(db)
    assert primary_report.failed == 0

    # Only the Warriors (and the Lakers decoy, which has no sibling squads)
    # are seeded here -- the other three multi-squad franchises' 6 targets
    # correctly report organization_missing rather than crashing the run.
    # test_populate_multi_squad_team_programs.py covers that reporting path
    # directly; here it is just the Warriors' 2 targets that matter.
    multi_squad_report = await run_multi_squad_population(db)
    assert multi_squad_report.failed == 0
    assert multi_squad_report.team_programs_created == 2
    assert multi_squad_report.organization_missing == 6

    return warriors_id


async def _seed_competition(db: AsyncSession) -> int:
    """Seed one minimal Summer League competition edition to satisfy the FK."""
    comp = SummerLeagueEdition(
        year=2026,
        league_id="test-multi-squad-league",
        venue_slug="test-multi-squad-venue",
        display_name="Test Multi-Squad Competition",
    )
    db.add(comp)
    await db.commit()
    comp_id = comp.id
    assert comp_id is not None
    return comp_id


@pytest.mark.asyncio
async def test_resolve_team_targets_second_and_third_squad_resolve_to_distinct_programs(
    db_session: AsyncSession,
) -> None:
    """A 17…/18… id resolves to a different team_program_id than its 16… parent,
    and the two siblings differ from each other -- against a real, populated DB.
    """
    warriors_id = await _seed_warriors_org_model(db_session)

    primary = await resolve_team_targets(db_session, nba_stats_team_id=GSW_PRIMARY_ID)
    gold = await resolve_team_targets(db_session, nba_stats_team_id=GSW_GOLD_ID)
    blue = await resolve_team_targets(db_session, nba_stats_team_id=GSW_BLUE_ID)

    # All three share the same franchise identity.
    assert primary[0] == gold[0] == blue[0] == warriors_id

    # But three distinct programs.
    assert None not in (primary[1], gold[1], blue[1])
    assert len({primary[1], gold[1], blue[1]}) == 3

    # Raw SQL per this repo's anti-vacuity guidance: confirm the specific
    # program identities, not merely "some" distinct ids.
    db_session.expunge_all()
    levels = dict(
        (
            await db_session.execute(
                text(
                    "SELECT id, level FROM team_programs "
                    "WHERE id = ANY(:ids)"
                ),
                {"ids": [primary[1], gold[1], blue[1]]},
            )
        ).all()
    )
    assert levels[primary[1]] == "NBA"
    assert levels[gold[1]] == "NBA-2"
    assert levels[blue[1]] == "NBA-3"


@pytest.mark.asyncio
async def test_two_entries_in_the_same_edition_resolve_to_two_distinct_programs(
    db_session: AsyncSession,
) -> None:
    """The key case: Warriors Gold and Warriors Blue, same edition, distinct programs.

    This is exactly the shape naive prefix-stripping onto one program would
    corrupt -- Gold and Blue play each other, so collapsing them would put
    the same franchise's roster on both sides of a game.
    """
    await _seed_warriors_org_model(db_session)
    comp_id = await _seed_competition(db_session)

    db_session.add_all(
        [
            SummerLeagueTeamEntry(
                competition_id=comp_id,
                nba_team_id=None,
                team_program_id=None,
                nba_stats_team_id=GSW_GOLD_ID,
                raw_team_name="Golden State Warriors Gold",
                team_slug="test-multi-squad-warriors-gold",
            ),
            SummerLeagueTeamEntry(
                competition_id=comp_id,
                nba_team_id=None,
                team_program_id=None,
                nba_stats_team_id=GSW_BLUE_ID,
                raw_team_name="Golden State Warriors Blue",
                team_slug="test-multi-squad-warriors-blue",
            ),
        ]
    )
    await db_session.commit()

    report = await run_backfill(db_session)
    assert report.stats_id.updated == 2
    assert report.stats_id.unresolvable == 0
    assert report.stats_id.uncovered == 0

    db_session.expunge_all()
    rows = (
        await db_session.execute(
            text(
                "SELECT nba_stats_team_id, nba_team_id, team_program_id "
                "FROM summer_league_team_entries "
                "WHERE competition_id = :competition_id "
                "ORDER BY nba_stats_team_id"
            ),
            {"competition_id": comp_id},
        )
    ).all()
    assert len(rows) == 2
    by_stats_id = {row[0]: (row[1], row[2]) for row in rows}

    gold_nba_team_id, gold_program_id = by_stats_id[GSW_GOLD_ID]
    blue_nba_team_id, blue_program_id = by_stats_id[GSW_BLUE_ID]

    # Same franchise identity...
    assert gold_nba_team_id == blue_nba_team_id
    assert gold_nba_team_id is not None
    # ...but two distinct programs, since they play each other.
    assert gold_program_id is not None
    assert blue_program_id is not None
    assert gold_program_id != blue_program_id


@pytest.mark.asyncio
async def test_franchise_page_resolver_still_resolves_the_primary_program(
    db_session: AsyncSession,
) -> None:
    """resolve_franchise_team_program_id must not go None once siblings exist.

    Regression guard: before scoping the query to PRIMARY_TEAM_PROGRAM_LEVEL,
    a franchise owning a second/third-squad sibling would make the ambiguity
    guard fire here too, silently blanking the Warriors franchise page (the
    fallback nba_team_id-only filter would then match neither the primary nor
    sibling entries once team_program_id is set on all of them).
    """
    await _seed_warriors_org_model(db_session)

    program_id = await resolve_franchise_team_program_id(
        db_session, nba_team_slug="warriors"
    )

    assert program_id is not None

    db_session.expunge_all()
    level = await db_session.scalar(
        text("SELECT level FROM team_programs WHERE id = :id"), {"id": program_id}
    )
    assert level == "NBA"


@pytest.mark.asyncio
async def test_genuinely_non_nba_ids_still_resolve_null_on_both_columns(
    db_session: AsyncSession,
) -> None:
    """Team China (45), Croatia (70), D-League Select (1612709916) stay NULL.

    Exercised against a real, populated multi-squad DB -- proves the new
    multi-squad map does not accidentally widen coverage to ids it should
    never touch.
    """
    await _seed_warriors_org_model(db_session)

    for stats_id in ("45", "70", "1612709916"):
        result = await resolve_team_targets(db_session, nba_stats_team_id=stats_id)
        assert result == (None, None)


@pytest.mark.asyncio
async def test_multi_squad_backfill_is_idempotent_on_rerun(
    db_session: AsyncSession,
) -> None:
    """Re-running the backfill for multi-squad entries updates nothing."""
    await _seed_warriors_org_model(db_session)
    comp_id = await _seed_competition(db_session)

    db_session.add(
        SummerLeagueTeamEntry(
            competition_id=comp_id,
            nba_team_id=None,
            team_program_id=None,
            nba_stats_team_id=GSW_GOLD_ID,
            raw_team_name="Golden State Warriors Gold",
            team_slug="test-multi-squad-idem-gold",
        )
    )
    await db_session.commit()

    first = await run_backfill(db_session)
    assert first.stats_id.updated == 1

    second = await run_backfill(db_session)
    assert second.stats_id.updated == 0
    assert second.stats_id.eligible == 0
