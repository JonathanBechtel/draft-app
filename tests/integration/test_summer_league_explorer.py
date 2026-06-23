"""Integration tests for the Summer League Explorer (Phase 1: players subject).

Explorer (`/stats/summer-league/explorer`): a faceted, URL-encoded query builder.
Covers query parsing/validation, players aggregation + scope filters, sorting,
pagination, the partial (JS-swap) render path, and the not-yet-available subjects.
"""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
    SummerLeagueTeamGameLog,
)
from app.schemas.summer_league_metrics import SummerLeagueMetricContext, SummerLeaguePlayerSeason
from app.services.summer_league_explorer_service import (
    ExplorerQuery,
    _PLAYER_ADVANCED_COLUMNS,
    _is_single_competition,
    parse_query,
    run_explorer_query,
)
from tests.integration.conftest import make_player

_N = {"i": 0}


async def _comp(db: AsyncSession, *, year: int, venue_slug: str, league_id: str) -> int:
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
    return comp.id


async def _team(db: AsyncSession, *, comp_id: int) -> SummerLeagueTeamEntry:
    _N["i"] += 1
    t = SummerLeagueTeamEntry(
        competition_id=comp_id,
        nba_stats_team_id=f"ex-team-{_N['i']}",
        raw_team_name="Team",
        team_slug=f"team-{_N['i']}",
    )
    db.add(t)
    await db.flush()
    return t


async def _log(
    db: AsyncSession,
    *,
    comp_id: int,
    team: SummerLeagueTeamEntry,
    player: PlayerMaster,
    pts: int,
    games: int = 1,
    round_label: str | None = None,
) -> None:
    """Add ``games`` identical box lines (30 MIN each) for one player."""
    for _ in range(games):
        _N["i"] += 1
        g = SummerLeagueGame(
            competition_id=comp_id,
            nba_stats_game_id=f"ex-game-{_N['i']}",
            game_date=date(2024, 7, 3),
            home_team_entry_id=team.id,
            away_team_entry_id=team.id,
            home_score=100,
            away_score=90,
            round_label=round_label,
        )
        db.add(g)
        await db.flush()
        assert g.id is not None
        sp = SummerLeagueSourcePlayer(
            nba_stats_person_id=f"ex-person-{_N['i']}",
            raw_player_name=player.display_name or "Player",
            normalized_name=(player.display_name or "player").lower(),
            canonical_player_id=player.id,
        )
        db.add(sp)
        await db.flush()
        db.add(
            SummerLeaguePlayerGameLog(
                competition_id=comp_id,
                game_id=g.id,
                team_entry_id=team.id,
                source_player_id=sp.id,
                player_id=player.id,
                nba_stats_person_id=sp.nba_stats_person_id,
                raw_player_name=player.display_name or "Player",
                minutes_seconds=1800,
                pts=pts,
                reb=5,
                ast=3,
                fgm=pts // 2,
                fga=pts,
            )
        )
    await db.flush()


async def _seed(db: AsyncSession) -> None:
    """Two players across two years/venues, each with 2 GP so they qualify.

    Scorer: 30 PPG (2024 Vegas). Roleplayer: 10 PPG (2025 Salt Lake).
    """
    scorer = make_player("Big", "Scorer")
    role = make_player("Role", "Player")
    scorer.draft_year, scorer.draft_round = 2024, 1
    role.draft_year, role.draft_round = 2025, 2
    db.add_all([scorer, role])
    await db.flush()

    c1 = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t1 = await _team(db, comp_id=c1)
    await _log(db, comp_id=c1, team=t1, player=scorer, pts=30, games=2)

    c2 = await _comp(db, year=2025, venue_slug="salt_lake_city", league_id="16")
    t2 = await _team(db, comp_id=c2)
    await _log(db, comp_id=c2, team=t2, player=role, pts=10, games=2)
    await db.commit()


# --------------------------------------------------------------------------- #
# parse_query
# --------------------------------------------------------------------------- #


def test_parse_query_defaults_and_validation() -> None:
    """Unknown subject/mode/sort fall back to safe defaults; ints coerce."""
    q = parse_query({"subject": "aliens", "mode": "warp", "sort": "evil", "dir": "x"})
    assert q.subject == "players"
    assert q.mode == "per_game"
    assert q.sort == "pts"
    assert q.direction == "desc"

    q2 = parse_query({"year_min": "2021", "year_max": "bad", "page": "-3"})
    assert q2.year_min == 2021
    assert q2.year_max is None  # invalid → filter off
    assert q2.page == 1  # clamped to >= 1


# --------------------------------------------------------------------------- #
# players subject
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_players_aggregates_and_sorts(db_session: AsyncSession) -> None:
    """Default players query returns qualifying players sorted by PTS desc."""
    await _seed(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="players"))

    assert result.available is True
    assert result.total == 2
    assert [r.label for r in result.rows] == ["Big Scorer", "Role Player"]
    assert result.rows[0].values["pts"] == 30.0
    assert result.rows[0].href == "/players/big-scorer"
    # Shooting volume is surfaced as discrete made/attempted columns.
    col_keys = {c.key for c in result.columns}
    assert {"fgm", "fga", "fg3m", "fg3a", "ftm", "fta"} <= col_keys
    assert "fgm" in result.rows[0].values


@pytest.mark.asyncio
async def test_players_venue_filter_narrows(db_session: AsyncSession) -> None:
    """Venue scope drops players who only appear at other venues."""
    await _seed(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", venue="las_vegas")
    )
    assert [r.label for r in result.rows] == ["Big Scorer"]


@pytest.mark.asyncio
async def test_players_draft_round_filter(db_session: AsyncSession) -> None:
    """Draft-round scope keeps only players from that round."""
    await _seed(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", draft_round=2)
    )
    assert [r.label for r in result.rows] == ["Role Player"]


@pytest.mark.asyncio
async def test_players_ascending_sort(db_session: AsyncSession) -> None:
    """Ascending sort flips the order."""
    await _seed(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", sort="pts", direction="asc")
    )
    assert [r.label for r in result.rows] == ["Role Player", "Big Scorer"]


@pytest.mark.asyncio
async def test_totals_mode_sums_box_score(db_session: AsyncSession) -> None:
    """Totals mode reports summed counting stats (30 PPG x 2 GP = 60 PTS)."""
    await _seed(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", mode="totals")
    )
    top = result.rows[0]
    assert top.label == "Big Scorer"
    assert top.values["pts"] == 60  # summed, not per-game


@pytest.mark.asyncio
async def test_per_36_mode_scales_by_minutes(db_session: AsyncSession) -> None:
    """Per-36 mode scales counting stats to a 36-minute rate (30 min played)."""
    await _seed(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", mode="per_36")
    )
    top = result.rows[0]
    # 60 total pts over 60 total min -> 1.0 pts/min -> 36.0 per 36.
    assert top.values["pts"] == 36.0


@pytest.mark.asyncio
async def test_min_minutes_filter_excludes_small_samples(
    db_session: AsyncSession,
) -> None:
    """A high min-minutes floor excludes everyone below it."""
    await _seed(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", min_minutes=10_000)
    )
    assert result.total == 0


# --------------------------------------------------------------------------- #
# subjects not yet available
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_facets_populate_for_every_subject(db_session: AsyncSession) -> None:
    """Builder facets populate regardless of subject."""
    await _seed(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="games"))
    assert result.facets.years == [2025, 2024]


# --------------------------------------------------------------------------- #
# teams subject (Phase 2)
# --------------------------------------------------------------------------- #


async def _team_game(
    db: AsyncSession,
    *,
    comp_id: int,
    home: SummerLeagueTeamEntry,
    away: SummerLeagueTeamEntry,
    home_score: int,
    away_score: int,
    pace: float = 100.0,
) -> None:
    """A real game between two distinct teams, with a box log per side."""
    _N["i"] += 1
    g = SummerLeagueGame(
        competition_id=comp_id,
        nba_stats_game_id=f"tm-game-{_N['i']}",
        game_date=date(2024, 7, 4),
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=home_score,
        away_score=away_score,
    )
    db.add(g)
    await db.flush()
    assert g.id is not None
    for entry, pts, opp in (
        (home, home_score, away_score),
        (away, away_score, home_score),
    ):
        db.add(
            SummerLeagueTeamGameLog(
                competition_id=comp_id,
                game_id=g.id,
                team_entry_id=entry.id,
                pts=pts,
                plus_minus=pts - opp,
                pace=pace,
                off_rating=110.0,
                def_rating=105.0,
            )
        )
    await db.flush()


async def _seed_teams(db: AsyncSession) -> None:
    """One competition, two teams; Alpha beats Bravo twice (Alpha 2-0, Bravo 0-2)."""
    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    alpha = await _team(db, comp_id=c)
    bravo = await _team(db, comp_id=c)
    alpha.raw_team_name, bravo.raw_team_name = "Alpha", "Bravo"
    await db.flush()
    await _team_game(
        db, comp_id=c, home=alpha, away=bravo, home_score=110, away_score=90
    )
    await _team_game(
        db, comp_id=c, home=bravo, away=alpha, home_score=95, away_score=105
    )
    await db.commit()


@pytest.mark.asyncio
async def test_teams_aggregates_record_from_scores(db_session: AsyncSession) -> None:
    """Teams subject computes W-L and scoring from game scores, sorted by DIFF."""
    await _seed_teams(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="teams"))

    assert result.available is True
    assert result.total == 2
    top = result.rows[0]
    assert top.label.startswith("Alpha")
    assert (top.values["w"], top.values["l"]) == (2, 0)
    assert top.values["ppg"] == 107.5  # (110 + 105) / 2
    assert top.values["opp_ppg"] == 92.5  # (90 + 95) / 2
    assert top.values["diff"] == 15.0
    assert top.values["pace"] == 100.0  # averaged from box logs
    # Bravo is the mirror image and sorts second (lower DIFF).
    assert result.rows[1].label.startswith("Bravo")
    assert result.rows[1].values["diff"] == -15.0


@pytest.mark.asyncio
async def test_teams_links_to_team_season(db_session: AsyncSession) -> None:
    """Each team row links to its team-season page."""
    await _seed_teams(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="teams"))
    assert result.rows[0].href is not None
    assert result.rows[0].href.startswith("/stats/summer-league/2024/las_vegas/")


@pytest.mark.asyncio
async def test_teams_default_sort_is_valid_for_subject(
    db_session: AsyncSession,
) -> None:
    """A player sort key passed to the teams subject falls back to the team default."""
    await _seed_teams(db_session)
    q = parse_query({"subject": "teams", "sort": "ts_pct"})
    assert q.sort == "diff"  # 'ts_pct' is not a team column


# --------------------------------------------------------------------------- #
# games subject (Phase 3)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_games_one_row_per_game_with_total_and_margin(
    db_session: AsyncSession,
) -> None:
    """Games subject yields one row per scored game with total + margin."""
    await _seed_teams(db_session)  # 2 games: 110-90 and 105-95
    result = await run_explorer_query(db_session, ExplorerQuery(subject="games"))

    assert result.available is True
    assert result.total == 2
    assert {r.values["total"] for r in result.rows} == {200}
    assert {r.values["margin"] for r in result.rows} == {20, 10}


@pytest.mark.asyncio
async def test_games_sort_by_margin(db_session: AsyncSession) -> None:
    """Sorting by margin desc puts the bigger blowout first, linked to its box."""
    await _seed_teams(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="games", sort="margin", direction="desc")
    )
    assert result.rows[0].values["margin"] == 20
    assert result.rows[0].href is not None
    assert result.rows[0].href.startswith("/stats/summer-league/2024/games/")
    # The label carries date + matchup + score.
    assert "@" in result.rows[0].label


@pytest.mark.asyncio
async def test_games_default_sort_is_total(db_session: AsyncSession) -> None:
    """The games subject defaults its sort key to 'total'."""
    q = parse_query({"subject": "games"})
    assert q.sort == "total"


@pytest.mark.asyncio
async def test_games_venue_filter(db_session: AsyncSession) -> None:
    """A non-matching venue filter yields no games."""
    await _seed_teams(db_session)  # all games at las_vegas
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="games", venue="orlando")
    )
    assert result.total == 0


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_explorer_page_renders(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """Full page renders the builder, subject tabs, and a seeded result."""
    await _seed(db_session)
    resp = await app_client.get("/stats/summer-league/explorer")
    assert resp.status_code == 200
    body = resp.text
    assert "Explorer" in body
    assert "explorer-form" in body
    assert "Big Scorer" in body


@pytest.mark.asyncio
async def test_explorer_partial_returns_table_only(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """?partial=1 returns just the results fragment (no full document)."""
    await _seed(db_session)
    resp = await app_client.get("/stats/summer-league/explorer?partial=1")
    assert resp.status_code == 200
    body = resp.text
    assert "explorer-results" in body
    assert "<html" not in body.lower()
    assert "explorer-form" not in body  # the builder is not in the partial


@pytest.mark.asyncio
async def test_explorer_not_shadowed_by_year_route(app_client: AsyncClient) -> None:
    """`/explorer` must hit the explorer route, not 422 against `/{year:int}`."""
    resp = await app_client.get("/stats/summer-league/explorer")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Phase 1: position filter, undrafted filter, positions facet, plus_minus
# --------------------------------------------------------------------------- #


async def _seed_with_positions(db: AsyncSession) -> None:
    """Three players: one G (drafted), one F (drafted), one undrafted with no position."""
    guard = make_player("Guard", "One")
    guard.position = "G"
    guard.draft_year, guard.draft_round = 2024, 1

    forward = make_player("Forward", "Two")
    forward.position = "F"
    forward.draft_year, forward.draft_round = 2024, 2

    undrafted = make_player("Undrafted", "Three")
    undrafted.position = None
    undrafted.draft_year = None

    db.add_all([guard, forward, undrafted])
    await db.flush()

    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db, comp_id=c)
    await _log(db, comp_id=c, team=t, player=guard, pts=20, games=2)
    await _log(db, comp_id=c, team=t, player=forward, pts=15, games=2)
    await _log(db, comp_id=c, team=t, player=undrafted, pts=10, games=2)
    await db.commit()


@pytest.mark.asyncio
async def test_position_filter_returns_only_matching_position(
    db_session: AsyncSession,
) -> None:
    """?position=G returns only players at that position; others are excluded."""
    await _seed_with_positions(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", position="G")
    )
    assert result.total == 1
    assert result.rows[0].label == "Guard One"


@pytest.mark.asyncio
async def test_undrafted_filter_returns_only_undrafted(
    db_session: AsyncSession,
) -> None:
    """?undrafted=1 returns only players with no draft year."""
    await _seed_with_positions(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", undrafted=True)
    )
    assert result.total == 1
    assert result.rows[0].label == "Undrafted Three"


@pytest.mark.asyncio
async def test_positions_facet_lists_distinct_values(
    db_session: AsyncSession,
) -> None:
    """Positions facet enumerates distinct non-null position values from PlayerMaster."""
    await _seed_with_positions(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="players"))
    assert "F" in result.facets.positions
    assert "G" in result.facets.positions
    # None is excluded from the facet
    assert None not in result.facets.positions  # type: ignore[operator]


@pytest.mark.asyncio
async def test_plus_minus_suppressed_in_rate_modes(
    db_session: AsyncSession,
) -> None:
    """plus_minus is None for per_36 and per_100 modes, present for per_game/totals."""
    await _seed(db_session)
    for mode in ("per_36", "per_100"):
        result = await run_explorer_query(
            db_session, ExplorerQuery(subject="players", mode=mode)
        )
        assert result.rows[0].values["plus_minus"] is None, f"expected None in {mode}"

    for mode in ("per_game", "totals"):
        result = await run_explorer_query(
            db_session, ExplorerQuery(subject="players", mode=mode)
        )
        assert result.rows[0].values["plus_minus"] is not None, (
            f"expected value in {mode}"
        )


# --------------------------------------------------------------------------- #
# Grain selector (Phase 2a-2d)
# --------------------------------------------------------------------------- #


async def _season(
    db: AsyncSession,
    *,
    player: PlayerMaster,
    comp_id: int,
    year: int,
    venue_slug: str,
    gp: int = 2,
    minutes: float = 60.0,
    pts: int = 20,
) -> None:
    """Add one SummerLeaguePlayerSeason row for a (player, competition)."""
    assert player.id is not None
    db.add(
        SummerLeaguePlayerSeason(
            competition_id=comp_id,
            player_id=player.id,
            year=year,
            venue_slug=venue_slug,
            gp=gp,
            minutes=minutes,
            pts=pts,
            reb=5,
            ast=3,
            fgm=pts // 2,
            fga=pts,
            fg3m=0,
            fg3a=0,
            ftm=0,
            fta=0,
            oreb=1,
            dreb=4,
            blk=1,
            stl=1,
            tov=2,
            pf=3,
            plus_minus=10,
        )
    )
    await db.flush()


async def _seed_grain(db: AsyncSession) -> tuple[PlayerMaster, int, int]:
    """One player, two competitions (Vegas 2024 and Salt Lake 2025).

    Returns (player, comp_id_vegas, comp_id_slc).
    """
    player = make_player("Star", "Player")
    db.add(player)
    await db.flush()

    c_vegas = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    c_slc = await _comp(db, year=2025, venue_slug="salt_lake_city", league_id="16")

    await _season(
        db, player=player, comp_id=c_vegas, year=2024, venue_slug="las_vegas", pts=30
    )
    await _season(
        db, player=player, comp_id=c_slc, year=2025, venue_slug="salt_lake_city", pts=10
    )
    await db.commit()
    return player, c_vegas, c_slc


@pytest.mark.asyncio
async def test_grain_per_competition_one_row_per_event(
    db_session: AsyncSession,
) -> None:
    """grain=per_competition yields one row per (player, competition) — 2 rows for 2 events."""
    await _seed_grain(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players", grain="per_competition", min_games=1, min_minutes=1
        ),
    )
    assert result.available is True
    assert result.total == 2
    # Labels carry venue name and year.
    labels = {r.label for r in result.rows}
    assert any("2024" in lbl for lbl in labels)
    assert any("2025" in lbl for lbl in labels)


@pytest.mark.asyncio
async def test_grain_per_game_one_row_per_log(
    db_session: AsyncSession,
) -> None:
    """grain=per_game yields one row per game log — 3 rows for 3 game logs."""
    player = make_player("Game", "Logger")
    db_session.add(player)
    await db_session.flush()

    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db_session, comp_id=c)
    await _log(db_session, comp_id=c, team=t, player=player, pts=20, games=3)
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="per_game"),
    )
    assert result.available is True
    assert result.total == 3
    # Each row links to the game box.
    for row in result.rows:
        assert row.href is not None
        assert "/stats/summer-league/2024/games/" in row.href


@pytest.mark.asyncio
async def test_grain_career_default_unchanged(
    db_session: AsyncSession,
) -> None:
    """No grain param (or grain=career) falls back to career aggregates (one row per player)."""
    await _seed(db_session)
    # No grain param → defaults to career.
    result_default = await run_explorer_query(
        db_session, ExplorerQuery(subject="players")
    )
    result_explicit = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", grain="career")
    )
    assert result_default.total == result_explicit.total == 2
    # Career grain returns one row per player (not per competition).
    labels = {r.label for r in result_default.rows}
    assert "Big Scorer" in labels
    assert "Role Player" in labels


@pytest.mark.asyncio
async def test_grain_parse_query_valid_grains() -> None:
    """parse_query accepts career/per_competition/per_game and rejects invalid values."""
    assert parse_query({"grain": "career"}).grain == "career"
    assert parse_query({"grain": "per_competition"}).grain == "per_competition"
    assert parse_query({"grain": "per_game"}).grain == "per_game"
    assert parse_query({"grain": "invalid"}).grain == "career"
    assert parse_query({}).grain == "career"


# --------------------------------------------------------------------------- #
# Phase 2e: draft pick range filter
# --------------------------------------------------------------------------- #


async def _seed_with_picks(db: AsyncSession) -> None:
    """Two drafted players with different pick numbers (5 and 25)."""
    early = make_player("Early", "Pick")
    early.draft_year, early.draft_round, early.draft_pick = 2024, 1, 5

    late = make_player("Late", "Pick")
    late.draft_year, late.draft_round, late.draft_pick = 2024, 1, 25

    db.add_all([early, late])
    await db.flush()

    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db, comp_id=c)
    await _log(db, comp_id=c, team=t, player=early, pts=20, games=2)
    await _log(db, comp_id=c, team=t, player=late, pts=15, games=2)
    await db.commit()


@pytest.mark.asyncio
async def test_draft_pick_range_filter(db_session: AsyncSession) -> None:
    """draft_pick_min/max narrows to players whose pick falls in the range."""
    await _seed_with_picks(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", draft_pick_min=1, draft_pick_max=10),
    )
    assert result.total == 1
    assert result.rows[0].label == "Early Pick"


# --------------------------------------------------------------------------- #
# Phase 2f: country / birth country filter
# --------------------------------------------------------------------------- #


async def _seed_with_countries(db: AsyncSession) -> None:
    """Two players with different birth countries (US and FR)."""
    us_player = make_player("USA", "Player")
    us_player.birth_country = "US"

    fr_player = make_player("France", "Player")
    fr_player.birth_country = "FR"

    db.add_all([us_player, fr_player])
    await db.flush()

    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db, comp_id=c)
    await _log(db, comp_id=c, team=t, player=us_player, pts=20, games=2)
    await _log(db, comp_id=c, team=t, player=fr_player, pts=15, games=2)
    await db.commit()


@pytest.mark.asyncio
async def test_country_filter(db_session: AsyncSession) -> None:
    """?country=US returns only players born in the US; FR player is excluded.

    Also asserts that the countries facet lists both seeded countries.
    """
    await _seed_with_countries(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", country="US")
    )
    assert result.total == 1
    assert result.rows[0].label == "USA Player"
    # Facet includes both countries (facet is unfiltered)
    assert "US" in result.facets.countries
    assert "FR" in result.facets.countries


# --------------------------------------------------------------------------- #
# Phase 2h: team filter for players
# --------------------------------------------------------------------------- #


async def _seed_with_two_teams(
    db: AsyncSession,
) -> tuple[SummerLeagueTeamEntry, SummerLeagueTeamEntry]:
    """Two players each on a different team entry within the same competition."""
    player_a = make_player("Alpha", "Player")
    player_b = make_player("Bravo", "Player")
    db.add_all([player_a, player_b])
    await db.flush()

    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    team_a = await _team(db, comp_id=c)
    team_b = await _team(db, comp_id=c)

    await _log(db, comp_id=c, team=team_a, player=player_a, pts=20, games=2)
    await _log(db, comp_id=c, team=team_b, player=player_b, pts=15, games=2)
    await db.commit()
    return team_a, team_b


@pytest.mark.asyncio
async def test_team_filter(db_session: AsyncSession) -> None:
    """?team_slug=<slug> returns only players who played for that team."""
    team_a, _team_b = await _seed_with_two_teams(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", team_slug=team_a.team_slug),
    )
    assert result.total == 1
    assert result.rows[0].label == "Alpha Player"


# --------------------------------------------------------------------------- #
# Phase 2g: round type filter
# --------------------------------------------------------------------------- #


async def _seed_round_types(db: AsyncSession) -> None:
    """Two players: one in Qualifying games, one in Championship games.

    Each player has 2 GP so they exceed the default min_games threshold.
    """
    qualifier = make_player("Qual", "Player")
    champion = make_player("Champ", "Player")
    db.add_all([qualifier, champion])
    await db.flush()

    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db, comp_id=c)
    await _log(
        db,
        comp_id=c,
        team=t,
        player=qualifier,
        pts=10,
        games=2,
        round_label="Qualifying",
    )
    await _log(
        db,
        comp_id=c,
        team=t,
        player=champion,
        pts=20,
        games=2,
        round_label="Championship",
    )
    await db.commit()


@pytest.mark.asyncio
async def test_round_type_filter_players(db_session: AsyncSession) -> None:
    """round_type filter narrows player results to games with that round_label.

    Seeding one player in Qualifying games and one in Championship games, then
    filtering to Qualifying should return only the qualifying player.
    """
    await _seed_round_types(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", round_type="Qualifying", min_games=1),
    )
    assert result.total == 1
    assert result.rows[0].label == "Qual Player"


@pytest.mark.asyncio
async def test_round_type_filter_games(db_session: AsyncSession) -> None:
    """round_type filter on the games subject yields 0 rows when no match exists.

    All seeded games via _seed_teams have no round_label; filtering to
    'Championship' should return an empty result set.
    """
    await _seed_teams(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="games", round_type="Championship"),
    )
    assert result.total == 0


@pytest.mark.asyncio
async def test_round_types_facet_lists_values(db_session: AsyncSession) -> None:
    """Facets include distinct non-null round_label values from SummerLeagueGame.

    After seeding games with 'Qualifying' and 'Championship' labels, both values
    should appear in facets.round_types (alphabetically sorted), while None is
    excluded.
    """
    await _seed_round_types(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="players"))
    assert "Championship" in result.facets.round_types
    assert "Qualifying" in result.facets.round_types
    # Alphabetical order
    assert result.facets.round_types == sorted(result.facets.round_types)


# --------------------------------------------------------------------------- #
# Bug-fix coverage: filters on non-career grains + team rating round_type scope
# --------------------------------------------------------------------------- #


async def _seed_per_comp_filters(db: AsyncSession) -> None:
    """Two players seeded with season rows for per_competition filter tests.

    - early_pick: draft_pick=5, country=USA, team_slug recorded via primary_team_entry_id
    - late_pick:  draft_pick=25, country=France
    Both have 2 GP and 60 minutes so they pass default eligibility.
    """
    early = make_player("Early", "Comp")
    early.draft_year, early.draft_round, early.draft_pick = 2024, 1, 5
    early.birth_country = "USA"
    late = make_player("Late", "Comp")
    late.draft_year, late.draft_round, late.draft_pick = 2024, 1, 25
    late.birth_country = "France"
    db.add_all([early, late])
    await db.flush()

    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t_early = await _team(db, comp_id=c)
    t_late = await _team(db, comp_id=c)

    # Give each player a season row linked to their team.
    for player, team, pts in ((early, t_early, 20), (late, t_late, 10)):
        assert player.id is not None
        assert team.id is not None
        season = SummerLeaguePlayerSeason(
            competition_id=c,
            player_id=player.id,
            year=2024,
            venue_slug="las_vegas",
            gp=2,
            minutes=60.0,
            pts=pts,
            reb=3,
            ast=2,
            fgm=pts // 2,
            fga=pts,
            fg3m=0,
            fg3a=0,
            ftm=0,
            fta=0,
            oreb=1,
            dreb=2,
            blk=0,
            stl=1,
            tov=1,
            pf=2,
            plus_minus=5,
            primary_team_entry_id=team.id,
        )
        db.add(season)
    await db.flush()
    await db.commit()


@pytest.mark.asyncio
async def test_per_competition_draft_pick_filter(db_session: AsyncSession) -> None:
    """draft_pick_min/max filters apply in per_competition grain."""
    await _seed_per_comp_filters(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            draft_pick_max=10,
            min_games=1,
            min_minutes=1,
        ),
    )
    assert result.total == 1
    assert result.rows[0].label.startswith("Early")


@pytest.mark.asyncio
async def test_per_competition_country_filter(db_session: AsyncSession) -> None:
    """Country filter applies in per_competition grain."""
    await _seed_per_comp_filters(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            country="France",
            min_games=1,
            min_minutes=1,
        ),
    )
    assert result.total == 1
    assert result.rows[0].label.startswith("Late")


@pytest.mark.asyncio
async def test_per_competition_team_slug_filter(db_session: AsyncSession) -> None:
    """team_slug filter joins primary_team_entry_id in per_competition grain."""
    await _seed_per_comp_filters(db_session)
    # Look up the team slug that was created for the "Early" player's team.
    result_all = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            min_games=1,
            min_minutes=1,
        ),
    )
    assert result_all.total == 2
    # Grab a team slug from the facets and confirm filtering to it returns 1 row.
    slug = result_all.facets.teams[0]
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            team_slug=slug,
            min_games=1,
            min_minutes=1,
        ),
    )
    assert result.total == 1


@pytest.mark.asyncio
async def test_per_competition_min_minutes_unit(db_session: AsyncSession) -> None:
    """min_minutes is compared directly to the minutes column (already in minutes).

    A player with 60 minutes should satisfy min_minutes=60 but not min_minutes=61.
    """
    await _seed_per_comp_filters(db_session)
    result_pass = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            min_games=1,
            min_minutes=60,
        ),
    )
    result_fail = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            min_games=1,
            min_minutes=61,
        ),
    )
    assert result_pass.total == 2
    assert result_fail.total == 0


@pytest.mark.asyncio
async def test_per_game_draft_pick_filter(db_session: AsyncSession) -> None:
    """draft_pick_min/max applies in per_game grain."""
    early = make_player("Early", "Game")
    early.draft_year, early.draft_round, early.draft_pick = 2024, 1, 5
    late = make_player("Late", "Game")
    late.draft_year, late.draft_round, late.draft_pick = 2024, 1, 25
    db_session.add_all([early, late])
    await db_session.flush()

    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db_session, comp_id=c)
    await _log(db_session, comp_id=c, team=t, player=early, pts=20, games=1)
    await _log(db_session, comp_id=c, team=t, player=late, pts=10, games=1)
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="per_game", draft_pick_max=10),
    )
    assert result.total == 1
    assert result.rows[0].label.startswith("Early")


@pytest.mark.asyncio
async def test_per_game_country_filter(db_session: AsyncSession) -> None:
    """Country filter applies in per_game grain."""
    french = make_player("French", "Player")
    french.birth_country = "France"
    american = make_player("American", "Player")
    american.birth_country = "USA"
    db_session.add_all([french, american])
    await db_session.flush()

    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db_session, comp_id=c)
    await _log(db_session, comp_id=c, team=t, player=french, pts=15, games=1)
    await _log(db_session, comp_id=c, team=t, player=american, pts=15, games=1)
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="per_game", country="France"),
    )
    assert result.total == 1
    assert result.rows[0].label.startswith("French")


@pytest.mark.asyncio
async def test_per_game_team_slug_filter(db_session: AsyncSession) -> None:
    """team_slug filter joins team_entry_id in per_game grain."""
    player = make_player("Lone", "Scorer")
    db_session.add(player)
    await db_session.flush()

    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    t1 = await _team(db_session, comp_id=c)
    t2 = await _team(db_session, comp_id=c)
    await _log(db_session, comp_id=c, team=t1, player=player, pts=20, games=1)
    await db_session.commit()

    # Filter to t2 (no logs) should return nothing.
    assert t2.team_slug is not None
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="per_game", team_slug=t2.team_slug),
    )
    assert result.total == 0

    # Filter to t1 should return the one log.
    assert t1.team_slug is not None
    result2 = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="per_game", team_slug=t1.team_slug),
    )
    assert result2.total == 1


@pytest.mark.asyncio
async def test_per_game_round_type_filter(db_session: AsyncSession) -> None:
    """round_type filter applies in per_game grain."""
    player = make_player("Round", "Tester")
    db_session.add(player)
    await db_session.flush()

    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db_session, comp_id=c)
    await _log(
        db_session,
        comp_id=c,
        team=t,
        player=player,
        pts=10,
        games=1,
        round_label="Qualifying",
    )
    await _log(
        db_session,
        comp_id=c,
        team=t,
        player=player,
        pts=20,
        games=1,
        round_label="Championship",
    )
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="per_game", round_type="Qualifying"),
    )
    assert result.total == 1
    assert result.rows[0].values["pts"] == 10.0


@pytest.mark.asyncio
async def test_teams_round_type_scopes_rating_query(db_session: AsyncSession) -> None:
    """When round_type is set, team pace/ORtg/DRtg are averaged only over matching games.

    Seeds two rounds for the same team: Qualifying (pace=80) and Championship (pace=120).
    Filtering to Qualifying should show pace~80, not the all-round average of 100.
    """
    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    alpha = await _team(db_session, comp_id=c)
    bravo = await _team(db_session, comp_id=c)
    alpha.raw_team_name, bravo.raw_team_name = "Alpha", "Bravo"
    await db_session.flush()

    # Qualifying game — pace 80
    _N["i"] += 1
    g_qual = SummerLeagueGame(
        competition_id=c,
        nba_stats_game_id=f"rt-game-{_N['i']}",
        game_date=date(2024, 7, 4),
        home_team_entry_id=alpha.id,
        away_team_entry_id=bravo.id,
        home_score=100,
        away_score=90,
        round_label="Qualifying",
    )
    db_session.add(g_qual)
    await db_session.flush()
    assert g_qual.id is not None
    for entry in (alpha, bravo):
        db_session.add(
            SummerLeagueTeamGameLog(
                competition_id=c,
                game_id=g_qual.id,
                team_entry_id=entry.id,
                pts=100 if entry is alpha else 90,
                plus_minus=10 if entry is alpha else -10,
                pace=80.0,
                off_rating=108.0,
                def_rating=100.0,
            )
        )

    # Championship game — pace 120
    _N["i"] += 1
    g_champ = SummerLeagueGame(
        competition_id=c,
        nba_stats_game_id=f"rt-game-{_N['i']}",
        game_date=date(2024, 7, 8),
        home_team_entry_id=alpha.id,
        away_team_entry_id=bravo.id,
        home_score=110,
        away_score=95,
        round_label="Championship",
    )
    db_session.add(g_champ)
    await db_session.flush()
    assert g_champ.id is not None
    for entry in (alpha, bravo):
        db_session.add(
            SummerLeagueTeamGameLog(
                competition_id=c,
                game_id=g_champ.id,
                team_entry_id=entry.id,
                pts=110 if entry is alpha else 95,
                plus_minus=15 if entry is alpha else -15,
                pace=120.0,
                off_rating=115.0,
                def_rating=100.0,
            )
        )
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="teams", round_type="Qualifying"),
    )
    assert result.total == 2
    alpha_row = next(r for r in result.rows if r.label.startswith("Alpha"))
    # Only the Qualifying game (pace=80) should be averaged, not both games (100).
    assert alpha_row.values["pace"] == 80.0


@pytest.mark.asyncio
async def test_per_competition_year_and_venue_filters(db_session: AsyncSession) -> None:
    """year_min/year_max and venue filter branches apply in per_competition grain."""
    await _seed_grain(db_session)  # seeds Vegas 2024 + SLC 2025 for one player
    # year_min excludes the 2024 row; only 2025 remains.
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            year_min=2025,
            min_games=1,
            min_minutes=1,
        ),
    )
    assert result.total == 1
    assert "2025" in result.rows[0].label

    # venue filter keeps only Vegas rows.
    result2 = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            year_max=2024,
            venue="las_vegas",
            min_games=1,
            min_minutes=1,
        ),
    )
    assert result2.total == 1
    assert "las_vegas" in result2.rows[0].label or "Vegas" in result2.rows[0].label


@pytest.mark.asyncio
async def test_per_competition_undrafted_filter(db_session: AsyncSession) -> None:
    """undrafted=True keeps only players with no draft_year in per_competition grain."""
    undrafted = make_player("Undrafted", "Comp")
    undrafted.draft_year = None  # make_player always sets draft_year=2025; override it
    drafted = make_player("Drafted", "Comp")
    drafted.draft_year = 2024
    db_session.add_all([undrafted, drafted])
    await db_session.flush()

    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    for player, pts in ((undrafted, 15), (drafted, 20)):
        await _season(
            db_session,
            player=player,
            comp_id=c,
            year=2024,
            venue_slug="las_vegas",
            pts=pts,
        )
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            undrafted=True,
            min_games=1,
            min_minutes=1,
        ),
    )
    assert result.total == 1
    assert result.rows[0].label.startswith("Undrafted")


@pytest.mark.asyncio
async def test_per_game_year_and_pick_min_filters(db_session: AsyncSession) -> None:
    """year_min and draft_pick_min filter branches apply in per_game grain."""
    early = make_player("Early", "PG")
    early.draft_year, early.draft_round, early.draft_pick = 2024, 1, 3
    late = make_player("Late", "PG")
    late.draft_year, late.draft_round, late.draft_pick = 2024, 1, 25
    db_session.add_all([early, late])
    await db_session.flush()

    c1 = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    c2 = await _comp(db_session, year=2025, venue_slug="las_vegas", league_id="16")
    t1 = await _team(db_session, comp_id=c1)
    t2 = await _team(db_session, comp_id=c2)
    await _log(db_session, comp_id=c1, team=t1, player=early, pts=20, games=1)
    await _log(db_session, comp_id=c2, team=t2, player=late, pts=10, games=1)
    await db_session.commit()

    # year_min=2025 keeps only the 2025 log.
    result_year = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="per_game", year_min=2025),
    )
    assert result_year.total == 1
    assert result_year.rows[0].label.startswith("Late")

    # draft_pick_min=10 keeps only pick 25 (Early pick=3 is excluded).
    result_pick = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="per_game", draft_pick_min=10),
    )
    assert result_pick.total == 1
    assert result_pick.rows[0].label.startswith("Late")


# --------------------------------------------------------------------------- #
# Phase 3: SQL-level pagination boundary tests
# --------------------------------------------------------------------------- #


async def _seed_many_players(db: AsyncSession, n: int) -> None:
    """Seed ``n`` players, each with 2 game logs (so they qualify at default thresholds).

    All players are seeded in a single competition with pts=i (0-indexed) so
    that ordering by pts gives a deterministic sequence.
    """
    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db, comp_id=c)
    for i in range(n):
        p = make_player(f"Player{i:03d}", "Paged")
        db.add(p)
        await db.flush()
        # Two game logs per player (to meet default min_games=2).
        await _log(db, comp_id=c, team=t, player=p, pts=i, games=2)
    await db.commit()


async def _seed_many_game_logs(db: AsyncSession, n: int) -> None:
    """Seed a single player with ``n`` game logs (one per game) to test per_game pagination."""
    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db, comp_id=c)
    player = make_player("PagedGame", "Player")
    db.add(player)
    await db.flush()
    # Seed n individual game logs.
    await _log(db, comp_id=c, team=t, player=player, pts=20, games=n)
    await db.commit()


@pytest.mark.asyncio
async def test_career_grain_pagination_55_players(db_session: AsyncSession) -> None:
    """55 seeded players → page 1 = 50 rows, page 2 = 5 rows, total = 55, has_next correct.

    Validates that SQL LIMIT/OFFSET is applied correctly and that total reflects
    the full unsliced count.
    """
    await _seed_many_players(db_session, 55)

    # Page 1.
    result_p1 = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", page=1, min_games=2),
    )
    assert result_p1.total == 55
    assert len(result_p1.rows) == 50
    assert result_p1.has_next is True
    assert result_p1.page == 1

    # Page 2.
    result_p2 = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", page=2, min_games=2),
    )
    assert result_p2.total == 55
    assert len(result_p2.rows) == 5
    assert result_p2.has_next is False
    assert result_p2.page == 2


@pytest.mark.asyncio
async def test_per_game_grain_pagination_60_logs(db_session: AsyncSession) -> None:
    """60 game logs → page 1 = 50 rows, page 2 = 10 rows, total = 60, has_next correct.

    Validates SQL pagination for the per_game grain (highest row count in production).
    """
    await _seed_many_game_logs(db_session, 60)

    # Default min_games=2 would exclude a single player with 60 logs (gp=60, fine).
    # But min_minutes check: each log is 1800 sec = 30 min; 60 * 30 = 1800 total min >> 60.
    result_p1 = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="per_game", page=1),
    )
    assert result_p1.total == 60
    assert len(result_p1.rows) == 50
    assert result_p1.has_next is True

    result_p2 = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="per_game", page=2),
    )
    assert result_p2.total == 60
    assert len(result_p2.rows) == 10
    assert result_p2.has_next is False


@pytest.mark.asyncio
async def test_career_grain_sort_order_preserved_across_pages(
    db_session: AsyncSession,
) -> None:
    """Sort order is consistent across pages: page 1 top row > page 2 top row.

    Seeds 55 players with pts=0..54 (per log; 2 logs each → totals 0..108).
    Sorted desc by pts, page-1 top should be pts=54 (total=108), page-2 top lower.
    """
    await _seed_many_players(db_session, 55)

    result_p1 = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players", grain="career", sort="pts", direction="desc", page=1
        ),
    )
    result_p2 = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players", grain="career", sort="pts", direction="desc", page=2
        ),
    )
    # The top row of page 1 should have higher pts than the top row of page 2.
    p1_top = result_p1.rows[0].values["pts"]
    p2_top = result_p2.rows[0].values["pts"]
    assert p1_top is not None and p2_top is not None
    assert p1_top > p2_top  # type: ignore[operator]


@pytest.mark.asyncio
async def test_career_grain_asc_sort_page1_is_lowest(db_session: AsyncSession) -> None:
    """Ascending sort on career grain: page 1 row 1 has the lowest pts value."""
    await _seed_many_players(db_session, 55)

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players", grain="career", sort="pts", direction="asc", page=1
        ),
    )
    # With pts=0 (total) as the minimum seeded value.
    assert result.rows[0].values["pts"] == 0.0  # per_game mode: 0 pts / 2 gp = 0.0


# --------------------------------------------------------------------------- #
# Phase 4a: per_competition composites + adv_eligible gating
# --------------------------------------------------------------------------- #


async def _metric_context(
    db: AsyncSession,
    *,
    comp_id: int,
    year: int,
    venue_slug: str,
    adv_eligible: bool = True,
) -> None:
    """Seed a SummerLeagueMetricContext row for (comp_id, year, venue_slug)."""
    ctx = SummerLeagueMetricContext(
        competition_id=comp_id,
        year=year,
        venue_slug=venue_slug,
        adv_eligible=adv_eligible,
    )
    db.add(ctx)
    await db.flush()


async def _season_with_composites(
    db: AsyncSession,
    *,
    player: PlayerMaster,
    comp_id: int,
    year: int,
    venue_slug: str,
    gp: int = 3,
    minutes: float = 90.0,
    pts: int = 20,
    per: float = 18.5,
    ortg: float = 112.0,
    drtg: float = 104.0,
    bpm: float = 2.1,
    ws: float = 0.8,
    vorp: float = 0.4,
    adv_eligible: bool = True,
) -> None:
    """Seed a SummerLeaguePlayerSeason with composite columns populated."""
    assert player.id is not None
    db.add(
        SummerLeaguePlayerSeason(
            competition_id=comp_id,
            player_id=player.id,
            year=year,
            venue_slug=venue_slug,
            gp=gp,
            minutes=minutes,
            pts=pts,
            reb=5,
            ast=3,
            fgm=pts // 2,
            fga=pts,
            fg3m=0,
            fg3a=0,
            ftm=0,
            fta=0,
            oreb=1,
            dreb=4,
            blk=1,
            stl=1,
            tov=2,
            pf=3,
            plus_minus=10,
            per=per if adv_eligible else None,
            ortg=ortg if adv_eligible else None,
            drtg=drtg if adv_eligible else None,
            bpm=bpm if adv_eligible else None,
            ws=ws if adv_eligible else None,
            vorp=vorp if adv_eligible else None,
            adv_eligible=adv_eligible,
        )
    )
    await db.flush()


async def _seed_adv_single_comp(
    db: AsyncSession,
    *,
    adv_eligible: bool,
    year: int = 2024,
    venue_slug: str = "las_vegas",
) -> int:
    """One player + one competition, with a metric context row controlling eligibility.

    Returns the competition id.
    """
    player = make_player("Adv", "Tester")
    db.add(player)
    await db.flush()

    comp_id = await _comp(db, year=year, venue_slug=venue_slug, league_id="15")

    await _season_with_composites(
        db,
        player=player,
        comp_id=comp_id,
        year=year,
        venue_slug=venue_slug,
        adv_eligible=adv_eligible,
    )
    await _metric_context(
        db,
        comp_id=comp_id,
        year=year,
        venue_slug=venue_slug,
        adv_eligible=adv_eligible,
    )
    await db.commit()
    return comp_id


def test_is_single_competition_helper() -> None:
    """_is_single_competition returns True only when year_min==year_max and venue is set."""
    assert _is_single_competition(ExplorerQuery(year_min=2024, year_max=2024, venue="las_vegas"))
    assert not _is_single_competition(ExplorerQuery(year_min=2024, year_max=2025, venue="las_vegas"))
    assert not _is_single_competition(ExplorerQuery(year_min=2024, year_max=2024, venue=None))
    assert not _is_single_competition(ExplorerQuery(year_min=None, year_max=2024, venue="las_vegas"))
    assert not _is_single_competition(ExplorerQuery())  # no constraints at all


@pytest.mark.asyncio
async def test_adv_columns_present_when_eligible(db_session: AsyncSession) -> None:
    """Single-competition per_competition query with adv_eligible=True surfaces PER/BPM/WS/VORP.

    The result's columns list must include all _PLAYER_ADVANCED_COLUMNS, and each
    row must have non-None values for PER, ORtg, DRtg, BPM, WS, VORP.
    """
    await _seed_adv_single_comp(db_session, adv_eligible=True)

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            year_min=2024,
            year_max=2024,
            venue="las_vegas",
            min_games=1,
            min_minutes=1,
        ),
    )

    assert result.adv_eligible is True
    adv_keys = {c.key for c in _PLAYER_ADVANCED_COLUMNS}
    result_col_keys = {c.key for c in result.columns}
    assert adv_keys <= result_col_keys, f"missing advanced keys: {adv_keys - result_col_keys}"

    # Verify values are present on the row.
    assert result.total == 1
    row = result.rows[0]
    for key in ("per", "ortg", "drtg", "bpm", "ws", "vorp"):
        assert row.values.get(key) is not None, f"expected non-None value for {key!r}"


@pytest.mark.asyncio
async def test_adv_columns_absent_when_not_eligible(db_session: AsyncSession) -> None:
    """Single-competition per_competition query with adv_eligible=False: no composite columns.

    The result's adv_eligible must be False, and the column list must NOT include
    advanced keys. Row values for composite keys must be absent (or None).
    """
    await _seed_adv_single_comp(db_session, adv_eligible=False)

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            year_min=2024,
            year_max=2024,
            venue="las_vegas",
            min_games=1,
            min_minutes=1,
        ),
    )

    assert result.adv_eligible is False
    adv_keys = {c.key for c in _PLAYER_ADVANCED_COLUMNS}
    result_col_keys = {c.key for c in result.columns}
    assert adv_keys.isdisjoint(result_col_keys), (
        f"unexpected advanced keys in columns: {adv_keys & result_col_keys}"
    )

    # Row values should not include non-None composite values.
    assert result.total == 1
    row = result.rows[0]
    for key in ("per", "ortg", "drtg", "bpm", "ws", "vorp"):
        assert row.values.get(key) is None, f"expected None for {key!r} when not eligible"


@pytest.mark.asyncio
async def test_adv_columns_absent_multi_year(db_session: AsyncSession) -> None:
    """Multi-year query (year_min != year_max): no composite columns regardless of eligibility.

    Composites are pool-calibrated and must not be exposed when the query spans
    multiple competitions.
    """
    # Seed two competitions (2024 Vegas + 2025 Vegas), both adv_eligible.
    player = make_player("Multi", "Year")
    db_session.add(player)
    await db_session.flush()

    c1 = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    c2 = await _comp(db_session, year=2025, venue_slug="las_vegas", league_id="16")

    for comp_id, year in ((c1, 2024), (c2, 2025)):
        await _season_with_composites(
            db_session, player=player, comp_id=comp_id, year=year,
            venue_slug="las_vegas", adv_eligible=True,
        )
        await _metric_context(
            db_session, comp_id=comp_id, year=year,
            venue_slug="las_vegas", adv_eligible=True,
        )
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            year_min=2024,
            year_max=2025,
            venue="las_vegas",
            min_games=1,
            min_minutes=1,
        ),
    )

    # Multi-year: composites must be absent.
    assert result.adv_eligible is False
    adv_keys = {c.key for c in _PLAYER_ADVANCED_COLUMNS}
    result_col_keys = {c.key for c in result.columns}
    assert adv_keys.isdisjoint(result_col_keys)


@pytest.mark.asyncio
async def test_adv_columns_absent_all_venues(db_session: AsyncSession) -> None:
    """No-venue filter (all-venues query): composite columns absent even with adv_eligible pools.

    A query without a venue spans multiple competitions, so composites are not valid.
    """
    await _seed_adv_single_comp(db_session, adv_eligible=True)

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            year_min=2024,
            year_max=2024,
            venue=None,  # no venue → not a single competition
            min_games=1,
            min_minutes=1,
        ),
    )

    assert result.adv_eligible is False
    adv_keys = {c.key for c in _PLAYER_ADVANCED_COLUMNS}
    result_col_keys = {c.key for c in result.columns}
    assert adv_keys.isdisjoint(result_col_keys)


@pytest.mark.asyncio
async def test_adv_sort_by_per_when_eligible(db_session: AsyncSession) -> None:
    """Sorting by 'per' in a single adv-eligible competition returns rows in PER order.

    Seeds two players with different PER values; sort=per desc should put the
    higher PER first.
    """
    player_hi = make_player("High", "PER")
    player_lo = make_player("Low", "PER")
    db_session.add_all([player_hi, player_lo])
    await db_session.flush()

    comp_id = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")

    await _season_with_composites(
        db_session, player=player_hi, comp_id=comp_id, year=2024,
        venue_slug="las_vegas", per=25.0, adv_eligible=True,
    )
    await _season_with_composites(
        db_session, player=player_lo, comp_id=comp_id, year=2024,
        venue_slug="las_vegas", per=12.0, adv_eligible=True,
    )
    await _metric_context(
        db_session, comp_id=comp_id, year=2024, venue_slug="las_vegas", adv_eligible=True
    )
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            year_min=2024,
            year_max=2024,
            venue="las_vegas",
            sort="per",
            direction="desc",
            min_games=1,
            min_minutes=1,
        ),
    )

    assert result.adv_eligible is True
    assert result.total == 2
    assert result.rows[0].label.startswith("High"), (
        f"expected High PER first, got {result.rows[0].label!r}"
    )
    per_top = result.rows[0].values.get("per")
    per_bot = result.rows[1].values.get("per")
    assert per_top is not None and per_bot is not None
    assert per_top > per_bot  # type: ignore[operator]


@pytest.mark.asyncio
async def test_adv_banner_rendered_when_not_eligible(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """HTML response includes the warning banner class when adv_eligible is False."""
    await _seed_adv_single_comp(db_session, adv_eligible=False)

    resp = await app_client.get(
        "/stats/summer-league/explorer"
        "?subject=players&grain=per_competition"
        "&year_min=2024&year_max=2024&venue=las_vegas"
        "&min_gp=1&min_min=1"
    )
    assert resp.status_code == 200
    assert "slg-explorer-banner--warn" in resp.text


@pytest.mark.asyncio
async def test_adv_banner_absent_when_eligible(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """HTML response does NOT include the warning banner when adv_eligible is True."""
    await _seed_adv_single_comp(db_session, adv_eligible=True)

    resp = await app_client.get(
        "/stats/summer-league/explorer"
        "?subject=players&grain=per_competition"
        "&year_min=2024&year_max=2024&venue=las_vegas"
        "&min_gp=1&min_min=1"
    )
    assert resp.status_code == 200
    assert "slg-explorer-banner--warn" not in resp.text
