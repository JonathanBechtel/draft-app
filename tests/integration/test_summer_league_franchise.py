"""Integration tests for the Summer League franchise-history page.

Franchise (`/stats/summer-league/teams/{team}`): aggregates one NBA franchise's
SL entries across years/venues into an all-time record, by-season rows (each
linking to the team-season page), career leaders, and an all-players roster.
``{team}`` is the canonical ``nba_teams.slug``; non-franchise squads (null
``nba_team_id``) never get a page.

The ``dual-read`` block at the bottom is ticket #795's proof obligation: this
page is the first production reader to resolve its team target through
``app.services.player_affiliation`` rather than reading ``nba_team_id``
directly, and these tests are what say the SQL clause and the Python resolver
agree. They deliberately re-read through raw SQL and ``expunge_all()`` -- the
``db_session`` fixture is ``expire_on_commit=False``, so an ORM object left in
the identity map would happily report the value the test *set* rather than the
value the database *stored*.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.nba_teams import NbaTeam
from app.schemas.organization import Organization, OrgKind, TeamProgram
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
)
from app.services.backbone.team_program_resolution import (
    PRIMARY_TEAM_PROGRAM_LEVEL,
    derive_org_slug,
    resolve_franchise_team_program_id,
)
from app.services.player_affiliation import (
    AffiliationTargetRef,
    NbaTeamRef,
    TeamProgramRef,
    resolve_team_target,
)
from app.services.summer_league_franchise_service import get_franchise_history
from tests.integration.conftest import make_player

_N = {"i": 0}


async def _entry(
    db: AsyncSession,
    *,
    comp_id: int,
    franchise: NbaTeam | None,
    name: str,
    team_program_id: int | None = None,
) -> SummerLeagueTeamEntry:
    """Seed one team entry, optionally carrying a generic-org-model target.

    ``franchise=None`` seeds an entry with no legacy ``nba_team_id`` (a select
    or non-NBA squad); ``team_program_id`` sets the dual-read program target
    independently, so any point of the ``(team_program_id, nba_team_id)``
    truth table can be constructed.
    """
    _N["i"] += 1
    t = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_team_id=franchise.id if franchise is not None else None,
        team_program_id=team_program_id,
        nba_stats_team_id=str(1610612747 + _N["i"]),
        raw_team_name=name,
        raw_team_abbreviation="LAL",
        team_slug=f"lakers-{_N['i']}",
    )
    db.add(t)
    await db.flush()
    return t


async def _game(
    db: AsyncSession,
    *,
    comp_id: int,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    home_score: int,
    away_score: int,
    game_date: date,
    log_player: PlayerMaster,
    log_team: SummerLeagueTeamEntry,
    pts: int = 20,
) -> None:
    _N["i"] += 1
    g = SummerLeagueGame(
        competition_id=comp_id,
        nba_stats_game_id=f"fr-game-{_N['i']}",
        game_date=game_date,
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=home_score,
        away_score=away_score,
    )
    db.add(g)
    await db.flush()
    assert g.id is not None
    sp = SummerLeagueSourceRecord(
        nba_stats_person_id=f"fr-person-{_N['i']}",
        raw_player_name=log_player.display_name or "Player",
        normalized_name=(log_player.display_name or "player").lower(),
        canonical_player_id=log_player.id,
    )
    db.add(sp)
    await db.flush()
    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=comp_id,
            game_id=g.id,
            team_entry_id=log_team.id,
            source_player_id=sp.id,
            player_id=log_player.id,
            nba_stats_person_id=sp.nba_stats_person_id,
            raw_player_name=log_player.display_name or "Player",
            minutes_seconds=1800,
            pts=pts,
            reb=8,
            ast=5,
            fgm=8,
            fga=15,
        )
    )
    await db.flush()


async def _comp(db: AsyncSession, *, year: int, venue_slug: str, league_id: str) -> int:
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
    return comp.id


async def _seed_franchise(
    db: AsyncSession, *, program_by_year: dict[int, int] | None = None
) -> tuple[NbaTeam, PlayerMaster]:
    """Two SL years for one franchise: 2024 (1-1) and 2025 (1-0). Star scores big.

    All-time record should be 2-1. The star (40 PPG) leads career scoring.

    Args:
        db: Async database session.
        program_by_year: Optional ``{year: team_program_id}``. A year present
            here has its franchise entry retargeted onto the generic org model
            (both targets set); a year absent stays legacy (``team_program_id``
            NULL). Passing neither, one, or both years produces the fully
            legacy, mixed, and fully retargeted populations respectively --
            every one of which must yield the identical page.
    """
    programs = program_by_year or {}
    franchise = NbaTeam(name="Los Angeles Lakers", abbreviation="LAL", slug="lakers")
    db.add(franchise)
    await db.flush()

    star = make_player("Star", "Wing")
    role = make_player("Role", "Player")
    db.add_all([star, role])
    await db.flush()

    # 2024 Vegas — Lakers split two games (1-1).
    c24 = await _comp(db, year=2024, venue_slug="vegas", league_id="15")
    lal24 = await _entry(
        db,
        comp_id=c24,
        franchise=franchise,
        name="Lakers",
        team_program_id=programs.get(2024),
    )
    opp24 = await _entry(db, comp_id=c24, franchise=franchise, name="Foes")
    # opp24 is a *different* franchise in reality; reuse table but detach below.
    opp24.nba_team_id = None
    await db.flush()
    await _game(
        db,
        comp_id=c24,
        home=lal24,
        away=opp24,
        home_score=100,
        away_score=90,
        game_date=date(2024, 7, 3),
        log_player=star,
        log_team=lal24,
        pts=40,
    )
    await _game(
        db,
        comp_id=c24,
        home=opp24,
        away=lal24,
        home_score=99,
        away_score=88,
        game_date=date(2024, 7, 5),
        log_player=role,
        log_team=lal24,
        pts=10,
    )

    # 2025 Vegas — Lakers win (1-0).
    c25 = await _comp(db, year=2025, venue_slug="vegas", league_id="15")
    lal25 = await _entry(
        db,
        comp_id=c25,
        franchise=franchise,
        name="Lakers",
        team_program_id=programs.get(2025),
    )
    opp25 = await _entry(db, comp_id=c25, franchise=franchise, name="Foes")
    opp25.nba_team_id = None
    await db.flush()
    await _game(
        db,
        comp_id=c25,
        home=lal25,
        away=opp25,
        home_score=110,
        away_score=95,
        game_date=date(2025, 7, 3),
        log_player=star,
        log_team=lal25,
        pts=40,
    )
    await db.commit()
    return franchise, star


@pytest.mark.asyncio
async def test_resolved_player_name_variants_collapse_to_one_row(
    db_session: AsyncSession,
) -> None:
    """A resolved player logged under two feed names aggregates into one row.

    Regression for the franchise aggregates splitting on ``raw_player_name``.
    """
    franchise = NbaTeam(name="Boston Celtics", abbreviation="BOS", slug="celtics")
    db_session.add(franchise)
    await db_session.flush()
    player = make_player("Jaylen", "Brown")
    db_session.add(player)
    await db_session.flush()

    comp_id = await _comp(db_session, year=2024, venue_slug="vegas", league_id="15")
    team = await _entry(db_session, comp_id=comp_id, franchise=franchise, name="Celtics")
    opp = await _entry(db_session, comp_id=comp_id, franchise=franchise, name="Foes")
    opp.nba_team_id = None
    await db_session.flush()

    # Same canonical player, two games, two different raw feed names.
    for i, raw_name in enumerate(("Jaylen Brown", "J. Brown")):
        g = SummerLeagueGame(
            competition_id=comp_id,
            nba_stats_game_id=f"var-game-{i}",
            game_date=date(2024, 7, 3 + i),
            home_team_entry_id=team.id,
            away_team_entry_id=opp.id,
            home_score=100,
            away_score=90,
        )
        db_session.add(g)
        await db_session.flush()
        sp = SummerLeagueSourceRecord(
            nba_stats_person_id=f"var-person-{i}",
            raw_player_name=raw_name,
            normalized_name=raw_name.lower(),
            canonical_player_id=player.id,
        )
        db_session.add(sp)
        await db_session.flush()
        db_session.add(
            SummerLeaguePlayerGameLog(
                competition_id=comp_id,
                game_id=g.id,
                team_entry_id=team.id,
                source_player_id=sp.id,
                player_id=player.id,
                nba_stats_person_id=sp.nba_stats_person_id,
                raw_player_name=raw_name,
                minutes_seconds=1800,
                pts=20,
            )
        )
    await db_session.commit()

    hist = await get_franchise_history(db_session, "celtics")

    assert hist is not None
    # One canonical player, not two — gp and points aggregated across both names.
    brown_rows = [p for p in hist.players if p.slug == player.slug]
    assert len(brown_rows) == 1
    assert brown_rows[0].gp == 2
    assert brown_rows[0].pts == 40
    assert hist.player_count == 1


@pytest.mark.asyncio
async def test_get_franchise_history_aggregates_record_and_players(
    db_session: AsyncSession,
) -> None:
    """All-time record sums across years; seasons sort newest-first; star leads."""
    franchise, star = await _seed_franchise(db_session)

    hist = await get_franchise_history(db_session, "lakers")

    assert hist is not None
    assert hist.name == "Los Angeles Lakers"
    assert (hist.all_time_wins, hist.all_time_losses) == (2, 1)
    assert hist.season_count == 2
    # Newest season first.
    assert [s.year for s in hist.seasons] == [2025, 2024]
    # By-season rows link via the per-competition team_slug, not the franchise.
    assert hist.seasons[0].team_slug.startswith("lakers-")
    # Star leads career scoring (40 + 40 = 80 pts over 2 GP).
    assert hist.leaders[0].slug == star.slug
    assert hist.leaders[0].pts == 80
    assert hist.leaders[0].seasons == 2
    # All players is alphabetical by name.
    names = [p.name for p in hist.players]
    assert names == sorted(names, key=str.lower)
    assert hist.player_count == 2


@pytest.mark.asyncio
async def test_franchise_page_renders(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """The franchise route renders the header, record, and a by-season link."""
    await _seed_franchise(db_session)

    resp = await app_client.get("/stats/summer-league/teams/lakers")

    assert resp.status_code == 200
    body = resp.text
    assert "Los Angeles Lakers" in body
    assert "2–1" in body  # all-time record
    assert "/stats/summer-league/2025/vegas/" in body  # by-season link


@pytest.mark.asyncio
async def test_franchise_page_unknown_returns_404(app_client: AsyncClient) -> None:
    """An unknown franchise slug 404s rather than rendering an empty page."""
    resp = await app_client.get("/stats/summer-league/teams/not-a-team")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_franchise_with_no_sl_appearances_returns_404(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """A real franchise that has never played Summer League 404s (no entries)."""
    db_session.add(NbaTeam(name="Expansion Team", abbreviation="EXP", slug="expansion"))
    await db_session.commit()

    assert await get_franchise_history(db_session, "expansion") is None
    resp = await app_client.get("/stats/summer-league/teams/expansion")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Dual-read team targets (#795)
# --------------------------------------------------------------------------- #


async def _seed_program(
    db: AsyncSession, *, nba_team_slug: str, program_slug: str | None = None
) -> int:
    """Create the org-model rows T3 creates for one franchise; return its program id.

    Keyed on ``derive_org_slug`` rather than a hand-written string so the test
    fixture and the production resolver share one definition of the natural
    key -- if that key ever changes these tests move with it, instead of
    silently exercising a franchise the reader can no longer find.

    ``level=PRIMARY_TEAM_PROGRAM_LEVEL`` mirrors what T3's population always
    writes (#810): ``resolve_franchise_team_program_id`` now scopes its query
    to that level so a franchise's second/third-squad sibling programs never
    make the ambiguity guard fire on the *primary* program lookup, so this
    fixture's program must carry the same level real T3 output does.
    """
    org = Organization(
        org_kind=OrgKind.CLUB,
        name=f"{nba_team_slug} org",
        slug=derive_org_slug(nba_team_slug),
    )
    db.add(org)
    await db.flush()
    program = TeamProgram(
        organization_id=org.id,
        name=f"{nba_team_slug} senior",
        slug=program_slug or derive_org_slug(nba_team_slug),
        level=PRIMARY_TEAM_PROGRAM_LEVEL,
    )
    db.add(program)
    await db.flush()
    assert program.id is not None
    return program.id


async def _stored_targets(
    db: AsyncSession, entry_id: int
) -> tuple[int | None, int | None]:
    """Read one entry's ``(team_program_id, nba_team_id)`` back through raw SQL.

    Anti-vacuity guard. ``SummerLeagueTeamEntry`` is a SQLModel, which silently
    discards constructor kwargs it does not recognise, and the ``db_session``
    fixture is ``expire_on_commit=False`` -- so asserting against the in-memory
    object could pass even if the column were never written. This bypasses the
    ORM entirely and asks the database what it actually holds.
    """
    row = (
        await db.execute(
            text(
                "SELECT team_program_id, nba_team_id "
                "FROM summer_league_team_entries WHERE id = :id"
            ),
            {"id": entry_id},
        )
    ).first()
    assert row is not None
    return (row[0], row[1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "years_retargeted"),
    [
        ("fully-retargeted", (2024, 2025)),
        ("fully-legacy", ()),
        ("mixed", (2024,)),
    ],
)
async def test_franchise_history_identical_across_target_populations(
    db_session: AsyncSession, label: str, years_retargeted: tuple[int, ...]
) -> None:
    """The page is identical whether entries carry a program target or not.

    The three populations the dual read has to survive: every entry retargeted
    onto the generic org model, every entry still legacy, and -- the case that
    catches an ``OR`` written wrong -- a franchise straddling both. All three
    must produce the same 2-1 all-time record over the same two seasons.
    """
    program_id = await _seed_program(db_session, nba_team_slug="lakers")
    franchise, star = await _seed_franchise(
        db_session, program_by_year={y: program_id for y in years_retargeted}
    )
    franchise_slug = franchise.slug
    star_slug = star.slug

    # The stored population is what the label claims -- read back from the
    # database, not from the objects the seeding code just built.
    db_session.expunge_all()
    franchise_entry_ids = list(
        (
            await db_session.execute(
                text(
                    "SELECT e.id FROM summer_league_team_entries e "
                    "JOIN summer_league_competitions c ON c.id = e.competition_id "
                    "WHERE e.nba_team_id IS NOT NULL ORDER BY c.year"
                )
            )
        ).scalars()
    )
    stored = [await _stored_targets(db_session, eid) for eid in franchise_entry_ids]
    assert [target[0] for target in stored] == [
        program_id if year in years_retargeted else None for year in (2024, 2025)
    ], f"{label}: the seeded population did not land in the database"

    db_session.expunge_all()
    hist = await get_franchise_history(db_session, franchise_slug)

    assert hist is not None, label
    assert (hist.all_time_wins, hist.all_time_losses) == (2, 1), label
    assert [s.year for s in hist.seasons] == [2025, 2024], label
    assert hist.season_count == 2, label
    assert hist.player_count == 2, label
    assert hist.leaders[0].slug == star_slug, label
    assert hist.leaders[0].pts == 80, label


@pytest.mark.asyncio
async def test_franchise_page_renders_for_a_mixed_population(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """End to end: the rendered page is unchanged for a straddling franchise."""
    program_id = await _seed_program(db_session, nba_team_slug="lakers")
    await _seed_franchise(db_session, program_by_year={2025: program_id})

    resp = await app_client.get("/stats/summer-league/teams/lakers")

    assert resp.status_code == 200
    body = resp.text
    assert "Los Angeles Lakers" in body
    assert "2–1" in body  # all-time record, unchanged by the dual read
    assert "/stats/summer-league/2025/vegas/" in body  # the retargeted season
    assert "/stats/summer-league/2024/vegas/" in body  # the legacy season


@pytest.mark.asyncio
async def test_clause_selects_exactly_what_resolve_team_target_selects(
    db_session: AsyncSession,
) -> None:
    """The SQL clause and the Python resolver agree over the whole truth table.

    Seeds every combination of ``(team_program_id, nba_team_id)`` a team entry
    can hold relative to two franchises and two programs, computes the expected
    membership by calling ``resolve_team_target`` on each row in Python, then
    asserts the page's SQL returned precisely that set.

    The load-bearing row is ``program-elsewhere-legacy-here``: retargeted to
    another program while still carrying this franchise's legacy
    ``nba_team_id``. The resolver assigns it to the other program, so it must
    not appear here -- and a clause written as the obvious
    ``team_program_id = :p OR nba_team_id = :n`` includes it and fails.
    """
    lakers = NbaTeam(name="Los Angeles Lakers", abbreviation="LAL", slug="lakers")
    celtics = NbaTeam(name="Boston Celtics", abbreviation="BOS", slug="celtics")
    db_session.add_all([lakers, celtics])
    await db_session.flush()
    lakers_id = lakers.id
    assert lakers_id is not None
    lakers_program = await _seed_program(db_session, nba_team_slug="lakers")
    celtics_program = await _seed_program(db_session, nba_team_slug="celtics")

    comp_id = await _comp(db_session, year=2026, venue_slug="vegas", league_id="15")
    truth_table: list[tuple[str, NbaTeam | None, int | None]] = [
        ("both-here", lakers, lakers_program),
        ("program-here-no-franchise", None, lakers_program),
        ("legacy-here-no-program", lakers, None),
        ("program-here-legacy-elsewhere", celtics, lakers_program),
        ("program-elsewhere-legacy-here", lakers, celtics_program),
        ("program-elsewhere-only", None, celtics_program),
        ("legacy-elsewhere", celtics, None),
        ("no-target-at-all", None, None),
    ]
    slug_by_case: dict[str, str] = {}
    id_by_case: dict[str, int] = {}
    for case, franchise, program_id in truth_table:
        entry = await _entry(
            db_session,
            comp_id=comp_id,
            franchise=franchise,
            name=case,
            team_program_id=program_id,
        )
        assert entry.id is not None
        slug_by_case[case] = entry.team_slug
        id_by_case[case] = entry.id
    await db_session.commit()

    # Expected membership, computed by the resolver itself over rows read back
    # out of Postgres -- never over the objects the seeding code constructed.
    db_session.expunge_all()
    targets: set[AffiliationTargetRef] = {
        TeamProgramRef(team_program_id=lakers_program),
        NbaTeamRef(nba_team_id=lakers_id),
    }
    expected: set[str] = set()
    for case, entry_id in id_by_case.items():
        team_program_id, nba_team_id = await _stored_targets(db_session, entry_id)
        probe = SummerLeagueTeamEntry(
            competition_id=comp_id,
            team_program_id=team_program_id,
            nba_team_id=nba_team_id,
            nba_stats_team_id="probe",
            raw_team_name=case,
            team_slug=slug_by_case[case],
        )
        if resolve_team_target(probe) in targets:
            expected.add(case)

    db_session.expunge_all()
    hist = await get_franchise_history(db_session, "lakers")

    assert hist is not None
    case_by_slug = {slug: case for case, slug in slug_by_case.items()}
    selected = {case_by_slug[s.team_slug] for s in hist.seasons}
    assert selected == expected

    # Non-vacuity: the resolver both accepted and rejected rows, and the
    # adversarial row landed on the rejected side rather than by luck of an
    # all-empty or all-full expectation.
    assert expected == {
        "both-here",
        "program-here-no-franchise",
        "legacy-here-no-program",
        "program-here-legacy-elsewhere",
    }
    assert "program-elsewhere-legacy-here" not in selected


@pytest.mark.asyncio
async def test_franchise_history_when_the_org_model_has_no_program_yet(
    db_session: AsyncSession,
) -> None:
    """Pre-population, the reader falls back to the legacy target with no change.

    ``resolve_franchise_team_program_id`` returns ``None`` and the clause
    degenerates to the ``nba_team_id`` filter this page has always run -- the
    property that lets this ship ahead of a completed backfill.
    """
    await _seed_franchise(db_session)

    db_session.expunge_all()
    assert (
        await resolve_franchise_team_program_id(db_session, nba_team_slug="lakers")
        is None
    )

    hist = await get_franchise_history(db_session, "lakers")

    assert hist is not None
    assert (hist.all_time_wins, hist.all_time_losses) == (2, 1)
    assert hist.season_count == 2


@pytest.mark.asyncio
async def test_franchise_history_when_the_organization_is_ambiguous(
    db_session: AsyncSession,
) -> None:
    """An organization owning two primary-level programs resolves to ``None``,
    never a guess.

    Per this repo's entity-resolution rule the reader refuses to pick one, and
    the page still renders off the legacy target rather than 404ing or showing
    an arbitrary half of the franchise's history.

    Both programs share ``PRIMARY_TEAM_PROGRAM_LEVEL`` ("NBA") (#810): that is
    what makes this a genuine same-level duplicate the ambiguity guard must
    still refuse, as opposed to a legitimate multi-squad sibling at a
    different level, which the #810 rescoped query now excludes from this
    guard entirely.
    """
    org = Organization(
        org_kind=OrgKind.CLUB, name="Lakers org", slug=derive_org_slug("lakers")
    )
    db_session.add(org)
    await db_session.flush()
    db_session.add_all(
        [
            TeamProgram(
                organization_id=org.id,
                name="Lakers senior",
                slug="nba-lakers",
                level=PRIMARY_TEAM_PROGRAM_LEVEL,
            ),
            TeamProgram(
                organization_id=org.id,
                name="Lakers senior duplicate",
                slug="nba-lakers-duplicate",
                level=PRIMARY_TEAM_PROGRAM_LEVEL,
            ),
        ]
    )
    await db_session.flush()
    await _seed_franchise(db_session)

    db_session.expunge_all()
    assert (
        await resolve_franchise_team_program_id(db_session, nba_team_slug="lakers")
        is None
    )

    hist = await get_franchise_history(db_session, "lakers")

    assert hist is not None
    assert (hist.all_time_wins, hist.all_time_losses) == (2, 1)
    assert hist.season_count == 2
