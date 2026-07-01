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
from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeaguePlayerSeason,
)
from app.services.summer_league_explorer_service import (
    ExplorerQuery,
    _PLAYER_ADVANCED_COLUMNS,
    _is_single_competition,
    parse_query,
    rollup_rate_composite,
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
        # starts_on/ends_on are intentionally left NULL to mirror production data
        # (ingested competitions have no dates). Career-grain age must derive from
        # the integer comp.year, not EXTRACT(YEAR FROM starts_on).
        starts_on=None,
        ends_on=None,
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
    Season rows are seeded alongside game logs so both career grain (season table,
    ticket #405) and per_game grain (game logs) work from the same fixture.
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
    # Season row: total of 2 × 30 pts = 60 pts, 2 × 30 min = 60 min.
    await _season(db, player=scorer, comp_id=c1, year=2024, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=60)

    c2 = await _comp(db, year=2025, venue_slug="salt_lake_city", league_id="16")
    t2 = await _team(db, comp_id=c2)
    await _log(db, comp_id=c2, team=t2, player=role, pts=10, games=2)
    # Season row: total of 2 × 10 pts = 20 pts, 2 × 30 min = 60 min.
    await _season(db, player=role, comp_id=c2, year=2025, venue_slug="salt_lake_city",
                  gp=2, minutes=60.0, pts=20)
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


@pytest.mark.asyncio
async def test_teams_advanced_columns(db_session: AsyncSession) -> None:
    """Teams subject exposes ORtg/DRtg/Pace/NetRtg derived from box-log averages.

    _seed_teams creates logs with off_rating=110.0, def_rating=105.0, pace=100.0
    for both teams in both games. Verified: NetRtg = ORtg - DRtg = 5.0; all four
    columns are present, populated, and accepted as sort keys.
    """
    await _seed_teams(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="teams"))

    # All four advanced columns appear in the column list.
    col_keys = {c.key for c in result.columns}
    assert {"pace", "ortg", "drtg", "net_rtg"} <= col_keys

    # Both teams have the same seeded ratings; check via the first row.
    row = result.rows[0]
    assert row.values["ortg"] == 110.0
    assert row.values["drtg"] == 105.0
    assert row.values["pace"] == 100.0
    # NetRtg is derived in Python as ORtg - DRtg.
    assert row.values["net_rtg"] == 5.0

    # net_rtg is accepted as a valid sort key for the teams subject.
    q_net = parse_query({"subject": "teams", "sort": "net_rtg"})
    assert q_net.sort == "net_rtg"

    # Sorting by net_rtg desc returns a non-empty result without error.
    result_sorted = await run_explorer_query(
        db_session, ExplorerQuery(subject="teams", sort="net_rtg", direction="desc")
    )
    assert result_sorted.total > 0
    assert result_sorted.rows[0].values["net_rtg"] == 5.0


@pytest.mark.asyncio
async def test_teams_advanced_missing_inputs_graceful(db_session: AsyncSession) -> None:
    """Teams with no box-log data produce None for all rating columns without error.

    Seeds a game with scores but no SummerLeagueTeamGameLog rows. The ratings
    query returns no rows for those entries, so pace/ortg/drtg/net_rtg should all
    degrade to None rather than 0 or raising.
    """
    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    alpha = await _team(db_session, comp_id=c)
    bravo = await _team(db_session, comp_id=c)
    alpha.raw_team_name, bravo.raw_team_name = "Alpha", "Bravo"
    await db_session.flush()

    # Game with scores but NO team box logs — ratings will be absent.
    _N["i"] += 1
    g = SummerLeagueGame(
        competition_id=c,
        nba_stats_game_id=f"nolog-game-{_N['i']}",
        game_date=date(2024, 7, 4),
        home_team_entry_id=alpha.id,
        away_team_entry_id=bravo.id,
        home_score=100,
        away_score=90,
    )
    db_session.add(g)
    await db_session.commit()

    result = await run_explorer_query(db_session, ExplorerQuery(subject="teams"))
    assert result.available is True
    assert result.total == 2

    for row in result.rows:
        # All rating-derived columns degrade to None, not 0/NaN.
        assert row.values["pace"] is None
        assert row.values["ortg"] is None
        assert row.values["drtg"] is None
        assert row.values["net_rtg"] is None


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
async def test_csv_export_returns_full_result_set(
    db_session: AsyncSession,
    app_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`?format=csv` returns every matching row, not just the current page.

    Regression for #393: the CSV branch previously reused the page-limited
    result. With PAGE_SIZE forced to 2 and three qualifying players seeded, the
    HTML page shows a single page (2 rows) while the CSV contains all 3 rows
    plus the header.
    """
    import app.services.summer_league_explorer_service as svc

    monkeypatch.setattr(svc, "PAGE_SIZE", 2)

    # Three qualifying players in one competition (career grain, default scope).
    players = [make_player(f"Csv{i}", "Player") for i in range(3)]
    for p in players:
        p.draft_year, p.draft_round = 2024, 1
    db_session.add_all(players)
    await db_session.flush()
    cid = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    team = await _team(db_session, comp_id=cid)
    for i, p in enumerate(players):
        await _log(db_session, comp_id=cid, team=team, player=p, pts=30 - i, games=2)
        # Season rows for career grain (ticket #405 source switch).
        await _season(db_session, player=p, comp_id=cid, year=2024,
                      venue_slug="las_vegas", gp=2, minutes=60.0, pts=(30 - i) * 2)
    await db_session.commit()

    # HTML page is paginated to PAGE_SIZE rows.
    html = await app_client.get("/stats/summer-league/explorer")
    assert html.status_code == 200
    assert html.text.count('scope="row"') == 2  # one page only
    assert "3 results" in html.text

    # CSV contains the header + every matching row.
    csv_resp = await app_client.get("/stats/summer-league/explorer?format=csv")
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_resp.headers["content-disposition"]
    lines = [ln for ln in csv_resp.text.splitlines() if ln.strip()]
    assert len(lines) == 1 + 3  # header + all three rows, not the 2-row page
    assert lines[0].startswith("Player,GP,MIN,PTS")


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
    # Season rows required for career grain (ticket #405 source switch).
    await _season(db, player=guard, comp_id=c, year=2024, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=40)
    await _season(db, player=forward, comp_id=c, year=2024, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=30)
    await _season(db, player=undrafted, comp_id=c, year=2024, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=20)
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
    primary_team_entry_id: int | None = None,
    ws: float | None = None,
    vorp: float | None = None,
    per: float | None = None,
    bpm: float | None = None,
    pace: float | None = None,
    adv_eligible: bool = False,
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
            primary_team_entry_id=primary_team_entry_id,
            ws=ws,
            vorp=vorp,
            per=per,
            bpm=bpm,
            pace=pace,
            adv_eligible=adv_eligible,
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
async def test_per_100_mode_per_competition_uses_pace(
    db_session: AsyncSession,
) -> None:
    """per_100 at per_competition grain derives possessions from the row's pace.

    pace is possessions per 48 minutes, so possessions = pace * minutes / 48 and
    the per-100 PTS cell = pts * 100 / possessions. A row without pace yields None
    (not 0, and not a /40 fallback), since possessions are unknown.
    """
    scorer = make_player("Pace", "Setter")
    noface = make_player("Nopace", "Row")
    db_session.add_all([scorer, noface])
    await db_session.flush()

    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    # 60 pts over 60 min at pace 100 (poss/48) → poss = 100*60/48 = 125 → 48.0 pts/100.
    await _season(
        db_session, player=scorer, comp_id=c, year=2024, venue_slug="las_vegas",
        gp=2, minutes=60.0, pts=60, pace=100.0,
    )
    await _season(
        db_session, player=noface, comp_id=c, year=2024, venue_slug="las_vegas",
        gp=2, minutes=60.0, pts=60, pace=None,
    )
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players", grain="per_competition", mode="per_100",
            min_games=1, min_minutes=1,
        ),
    )
    by_name = {r.label.split(" · ")[0]: r for r in result.rows}
    assert by_name["Pace Setter"].values["pts"] == pytest.approx(48.0, abs=0.05)
    assert by_name["Nopace Row"].values["pts"] is None


@pytest.mark.asyncio
async def test_per_100_mode_career_pools_pace_and_flags_partial(
    db_session: AsyncSession,
) -> None:
    """Career per_100 pools possessions over pace-covered competitions.

    Full coverage → exact (not flagged); mixed coverage → pooled over the paced
    competitions only and flagged per100_approx; no pace at all → None.
    """
    full = make_player("Full", "Cover")
    partial = make_player("Partial", "Cover")
    nopace = make_player("No", "Pace")
    db_session.add_all([full, partial, nopace])
    await db_session.flush()

    c1 = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    c2 = await _comp(db_session, year=2025, venue_slug="salt_lake_city", league_id="16")

    # 30 pts / 30 min at pace 100 → poss = 100*30/48 = 62.5 per competition.
    await _season(db_session, player=full, comp_id=c1, year=2024,
                  venue_slug="las_vegas", gp=2, minutes=30.0, pts=30, pace=100.0)
    await _season(db_session, player=full, comp_id=c2, year=2025,
                  venue_slug="salt_lake_city", gp=2, minutes=30.0, pts=30, pace=100.0)
    # Partial: one paced competition, one without pace.
    await _season(db_session, player=partial, comp_id=c1, year=2024,
                  venue_slug="las_vegas", gp=2, minutes=30.0, pts=30, pace=100.0)
    await _season(db_session, player=partial, comp_id=c2, year=2025,
                  venue_slug="salt_lake_city", gp=2, minutes=30.0, pts=30, pace=None)
    # No pace anywhere → per-100 unavailable.
    await _season(db_session, player=nopace, comp_id=c1, year=2024,
                  venue_slug="las_vegas", gp=2, minutes=30.0, pts=30, pace=None)
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players", grain="career", mode="per_100",
            min_games=1, min_minutes=1,
        ),
    )
    by_name = {r.label: r for r in result.rows}

    # Full coverage: 60 pts / 125 poss = 48.0, exact.
    assert by_name["Full Cover"].values["pts"] == pytest.approx(48.0, abs=0.05)
    assert by_name["Full Cover"].per100_approx is False
    # Partial: 60 pts pooled over the paced competition's 62.5 poss = 96.0, flagged.
    assert by_name["Partial Cover"].values["pts"] == pytest.approx(96.0, abs=0.05)
    assert by_name["Partial Cover"].per100_approx is True
    # No pace: possessions unknown → None, nothing to flag.
    assert by_name["No Pace"].values["pts"] is None
    assert by_name["No Pace"].per100_approx is False


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
async def test_gmsc_sorts_on_career_and_per_game_grains(
    db_session: AsyncSession,
) -> None:
    """GmSc is a valued, sortable base column at the career and per_game grains.

    Exercises the SUM-aggregate (career) and single-game (per_game) SQL sort
    expressions, so a bad fragment surfaces as a query error rather than a silent
    mis-sort.  _seed gives two players (30 vs 10 PPG) over game logs.
    """
    await _seed(db_session)

    career = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", sort="gmsc", direction="desc"),
    )
    assert career.total == 2
    assert "gmsc" in career.rows[0].values
    # GmSc is monotonic with the scoring gap here: Big Scorer (30) > Role Player (10).
    assert career.rows[0].label == "Big Scorer"
    assert career.rows[0].values["gmsc"] > career.rows[1].values["gmsc"]

    per_game = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players", grain="per_game", sort="gmsc", direction="desc"
        ),
    )
    assert per_game.total >= 1
    assert all("gmsc" in r.values for r in per_game.rows)


@pytest.mark.asyncio
async def test_gmsc_sorts_on_per_competition_grain(
    db_session: AsyncSession,
) -> None:
    """per_competition GmSc reads the materialized season rows and sorts on the raw-label expr.

    _seed_grain gives one player across two events; the 30-PTS Vegas line outscores
    the 10-PTS Salt Lake line, so it sorts first under gmsc desc.
    """
    await _seed_grain(db_session)
    per_comp = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            sort="gmsc",
            direction="desc",
            min_games=1,
            min_minutes=1,
        ),
    )
    assert per_comp.total == 2
    assert all("gmsc" in r.values for r in per_comp.rows)
    assert "2024" in per_comp.rows[0].label
    assert per_comp.rows[0].values["gmsc"] > per_comp.rows[1].values["gmsc"]


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
    # Season rows required for career grain (ticket #405 source switch).
    await _season(db, player=early, comp_id=c, year=2024, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=40)
    await _season(db, player=late, comp_id=c, year=2024, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=30)
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
    # Season rows required for career grain (ticket #405 source switch).
    await _season(db, player=us_player, comp_id=c, year=2024, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=40)
    await _season(db, player=fr_player, comp_id=c, year=2024, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=30)
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
    # Facet includes both countries (facet is unfiltered), canonicalized to
    # display names — raw ISO-2 codes (US/FR) are normalized for the dropdown.
    assert "United States" in result.facets.countries
    assert "France" in result.facets.countries


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
    # Season rows with primary_team_entry_id so team filter works at career grain.
    assert team_a.id is not None and team_b.id is not None
    await _season(db, player=player_a, comp_id=c, year=2024, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=40, primary_team_entry_id=team_a.id)
    await _season(db, player=player_b, comp_id=c, year=2024, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=30, primary_team_entry_id=team_b.id)
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
    Season rows are also seeded so career grain works (ticket #405 source switch).
    Note: round_type is not applicable at career grain — season rows aggregate a
    full competition regardless of round.  Round-type filtering at career grain
    was removed as part of the season-table source switch; use per_game grain for
    round-type-scoped results.
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
    # Season rows: career grain aggregates the full competition, no round distinction.
    await _season(db, player=qualifier, comp_id=c, year=2024, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=20)
    await _season(db, player=champion, comp_id=c, year=2024, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=40)
    await db.commit()


@pytest.mark.asyncio
async def test_round_type_filter_players(db_session: AsyncSession) -> None:
    """round_type is silently ignored at career grain; use per_game for round-scoped results.

    After ticket #405 the career grain reads summer_league_player_seasons (one row per
    full competition).  Season rows aggregate all games in a competition regardless of
    round, so a round_type filter cannot narrow career results.  Both the Qualifying
    and Championship players appear even when round_type='Qualifying' is set.
    For genuine round-type scoping see test_per_game_round_type_filter (per_game grain).
    """
    await _seed_round_types(db_session)
    # Career grain (default): round_type is ignored — both players appear.
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", round_type="Qualifying", min_games=1),
    )
    assert result.total == 2  # both players visible; round_type has no effect at career grain
    labels = {r.label for r in result.rows}
    assert "Qual Player" in labels
    assert "Champ Player" in labels


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
    Season rows are also seeded for career grain (ticket #405 source switch).
    """
    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db, comp_id=c)
    for i in range(n):
        p = make_player(f"Player{i:03d}", "Paged")
        db.add(p)
        await db.flush()
        # Two game logs per player (to meet default min_games=2).
        await _log(db, comp_id=c, team=t, player=p, pts=i, games=2)
        # Season row: total pts = i * 2 games, 2 × 30 min = 60 min.
        await _season(db, player=p, comp_id=c, year=2024, venue_slug="las_vegas",
                      gp=2, minutes=60.0, pts=i * 2)
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
    assert _is_single_competition(
        ExplorerQuery(year_min=2024, year_max=2024, venue="las_vegas")
    )
    assert not _is_single_competition(
        ExplorerQuery(year_min=2024, year_max=2025, venue="las_vegas")
    )
    assert not _is_single_competition(
        ExplorerQuery(year_min=2024, year_max=2024, venue=None)
    )
    assert not _is_single_competition(
        ExplorerQuery(year_min=None, year_max=2024, venue="las_vegas")
    )
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
    assert adv_keys <= result_col_keys, (
        f"missing advanced keys: {adv_keys - result_col_keys}"
    )

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
        assert row.values.get(key) is None, (
            f"expected None for {key!r} when not eligible"
        )


@pytest.mark.asyncio
async def test_adv_columns_present_multi_year(db_session: AsyncSession) -> None:
    """#406: Multi-year per_competition query now exposes advanced columns.

    Previously composites were hidden for multi-year queries; after ticket #406 they
    are always shown at per_competition grain (each row is one pool, values are
    exact within that pool).  adv_eligible is False (not single-comp) but the
    column list still includes advanced columns with the N-of-M banner providing
    the eligibility context.
    """
    # Seed two competitions (2024 Vegas + 2025 Vegas), both adv_eligible.
    player = make_player("Multi", "Year")
    db_session.add(player)
    await db_session.flush()

    c1 = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    c2 = await _comp(db_session, year=2025, venue_slug="las_vegas", league_id="16")

    for comp_id, year in ((c1, 2024), (c2, 2025)):
        await _season_with_composites(
            db_session,
            player=player,
            comp_id=comp_id,
            year=year,
            venue_slug="las_vegas",
            adv_eligible=True,
        )
        await _metric_context(
            db_session,
            comp_id=comp_id,
            year=year,
            venue_slug="las_vegas",
            adv_eligible=True,
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

    # Multi-comp: adv_eligible is False (not single-comp scope), but advanced
    # columns ARE present in the column list (ticket #406 change).
    assert result.adv_eligible is False
    adv_keys = {c.key for c in _PLAYER_ADVANCED_COLUMNS}
    result_col_keys = {c.key for c in result.columns}
    assert adv_keys <= result_col_keys, (
        f"missing advanced keys at multi-year per_competition: {adv_keys - result_col_keys}"
    )
    # Two rows (one per competition); each row's values for composites are non-None.
    assert result.total == 2
    for row in result.rows:
        assert row.values.get("per") is not None
        assert row.values.get("ws") is not None


@pytest.mark.asyncio
async def test_adv_columns_present_all_venues(db_session: AsyncSession) -> None:
    """#406: No-venue per_competition query now exposes advanced columns.

    Previously composite columns were hidden when no venue was specified.  After
    ticket #406 they are always shown at per_competition grain; the N-of-M banner
    provides eligibility context.
    """
    await _seed_adv_single_comp(db_session, adv_eligible=True)

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            year_min=2024,
            year_max=2024,
            venue=None,  # no venue → multi-comp scope
            min_games=1,
            min_minutes=1,
        ),
    )

    # adv_eligible is False (not single-comp), but columns ARE present now (#406).
    assert result.adv_eligible is False
    adv_keys = {c.key for c in _PLAYER_ADVANCED_COLUMNS}
    result_col_keys = {c.key for c in result.columns}
    assert adv_keys <= result_col_keys, (
        f"missing advanced keys at all-venues per_competition: {adv_keys - result_col_keys}"
    )


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
        db_session,
        player=player_hi,
        comp_id=comp_id,
        year=2024,
        venue_slug="las_vegas",
        per=25.0,
        adv_eligible=True,
    )
    await _season_with_composites(
        db_session,
        player=player_lo,
        comp_id=comp_id,
        year=2024,
        venue_slug="las_vegas",
        per=12.0,
        adv_eligible=True,
    )
    await _metric_context(
        db_session,
        comp_id=comp_id,
        year=2024,
        venue_slug="las_vegas",
        adv_eligible=True,
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


# --------------------------------------------------------------------------- #
# Phase 4c: age filter (career grain and per_competition grain)
# --------------------------------------------------------------------------- #
#
# Age computation choices (matches service layer inline comment):
#   - career grain: age = MIN(comp.year) - EXTRACT(YEAR FROM pm.birthdate)
#     → anchored to the EARLIEST competition in scope (youngest / debut-era age)
#   - per_competition grain: age = ps.year - EXTRACT(YEAR FROM pm.birthdate)
#     → competition year minus birth year, one row per season


async def _seed_age_filter(
    db: AsyncSession,
) -> tuple[PlayerMaster, PlayerMaster]:
    """Two players with known birth years and known debut years.

    young_player: born 2004-01-01, first SL in 2023 → career age = 19
    old_player:   born 1999-01-01, first SL in 2023 → career age = 24

    Both have 2 GP and 60 minutes so they qualify at default thresholds.
    Competition year is 2023 (starts_on is NULL, as in production).
    Season rows seeded alongside game logs for career grain (ticket #405 source switch).
    """
    young = make_player("Young", "Rookie")
    young.birthdate = date(2004, 1, 1)

    old = make_player("Old", "Vet")
    old.birthdate = date(1999, 1, 1)

    db.add_all([young, old])
    await db.flush()

    # Competition year is 2023 (ps.year is used in new impl, same value as comp.year).
    # young career age: 2023 - 2004 = 19
    # old   career age: 2023 - 1999 = 24
    c = await _comp(db, year=2023, venue_slug="las_vegas", league_id="15")
    t = await _team(db, comp_id=c)
    await _log(db, comp_id=c, team=t, player=young, pts=20, games=2)
    await _log(db, comp_id=c, team=t, player=old, pts=15, games=2)
    # Season rows: career grain reads these (ticket #405 source switch).
    await _season(db, player=young, comp_id=c, year=2023, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=40)
    await _season(db, player=old, comp_id=c, year=2023, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=30)
    await db.commit()
    return young, old


@pytest.mark.asyncio
async def test_age_min_filter_career_excludes_younger_players(
    db_session: AsyncSession,
) -> None:
    """age_min filter in career grain: only players old enough are returned.

    young_player (career age 19) must be excluded by age_min=20.
    old_player (career age 24) must be included.
    """
    await _seed_age_filter(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", age_min=20),
    )
    assert result.total == 1
    assert result.rows[0].label == "Old Vet"


@pytest.mark.asyncio
async def test_age_max_filter_career_excludes_older_players(
    db_session: AsyncSession,
) -> None:
    """age_max filter in career grain: only players young enough are returned.

    old_player (career age 24) must be excluded by age_max=21.
    young_player (career age 19) must be included.
    """
    await _seed_age_filter(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", age_max=21),
    )
    assert result.total == 1
    assert result.rows[0].label == "Young Rookie"


@pytest.mark.asyncio
async def test_age_range_filter_career_both_bounds(
    db_session: AsyncSession,
) -> None:
    """age_min and age_max together narrow to players within the range (both inclusive).

    age_min=19, age_max=21 → only young_player (age 19) qualifies.
    """
    await _seed_age_filter(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", age_min=19, age_max=21),
    )
    assert result.total == 1
    assert result.rows[0].label == "Young Rookie"


@pytest.mark.asyncio
async def test_age_filter_career_no_bounds_returns_all(
    db_session: AsyncSession,
) -> None:
    """No age filter returns all qualifying players (both bounds None)."""
    await _seed_age_filter(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career"),
    )
    assert result.total == 2


@pytest.mark.asyncio
async def test_age_filter_career_anchors_to_earliest_competition(
    db_session: AsyncSession,
) -> None:
    """Career-grain age is anchored to the player's EARLIEST competition, not latest.

    One player (born 2004-01-01) appears in two SL competitions: 2023 and 2025.
    Earliest-anchor age = 2023 - 2004 = 19; latest-anchor age = 2025 - 2004 = 21.
    Filtering age_max=20 must INCLUDE the player (19 <= 20). If the implementation
    anchored to MAX(ps.year) instead of MIN, the age would read 21 and the player
    would be wrongly excluded — so this distinguishes the two semantics.
    """
    player = make_player("Two", "Comps")
    player.birthdate = date(2004, 1, 1)
    db_session.add(player)
    await db_session.flush()

    c_early = await _comp(db_session, year=2023, venue_slug="las_vegas", league_id="15")
    c_late = await _comp(db_session, year=2025, venue_slug="las_vegas", league_id="15")
    t_early = await _team(db_session, comp_id=c_early)
    t_late = await _team(db_session, comp_id=c_late)
    await _log(
        db_session, comp_id=c_early, team=t_early, player=player, pts=20, games=2
    )
    await _log(db_session, comp_id=c_late, team=t_late, player=player, pts=18, games=2)
    # Season rows for both competitions (career grain reads ps.year for age).
    await _season(db_session, player=player, comp_id=c_early, year=2023,
                  venue_slug="las_vegas", gp=2, minutes=60.0, pts=40)
    await _season(db_session, player=player, comp_id=c_late, year=2025,
                  venue_slug="las_vegas", gp=2, minutes=60.0, pts=36)
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", age_max=20),
    )
    assert result.total == 1, "earliest-era age (19) should pass age_max=20"
    assert result.rows[0].label == "Two Comps"


@pytest.mark.asyncio
async def test_age_filter_applies_to_per_game_grain(
    db_session: AsyncSession,
) -> None:
    """#398 codex: the age filter must also constrain grain=per_game.

    _seed_age_filter seeds a 19-year-old and a 24-year-old (at the 2023 competition),
    each with game logs. With grain=per_game and age_max=21, only the young player's
    game-log rows may appear; previously the per_game builder ignored age entirely.
    """
    young, old = await _seed_age_filter(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_game",
            age_max=21,
            min_games=1,
            min_minutes=1,
        ),
    )
    assert result.total >= 1
    names = {r.label.split(" · ")[0] for r in result.rows}
    assert names == {"Young Rookie"}, f"old vet (age 24) should be excluded: {names}"


@pytest.mark.asyncio
async def test_age_filter_career_no_birthdate_excluded(
    db_session: AsyncSession,
) -> None:
    """A player with NULL birthdate is excluded when age_min is set.

    NULL birthday → NULL computed age → does not satisfy any age bound.
    The other player (with a known birthdate) is still returned.
    """
    no_birth = make_player("No", "Birth")
    no_birth.birthdate = None
    has_birth = make_player("Has", "Birth")
    has_birth.birthdate = date(2000, 6, 1)  # career age = 2023 - 2000 = 23
    db_session.add_all([no_birth, has_birth])
    await db_session.flush()

    c = await _comp(db_session, year=2023, venue_slug="las_vegas", league_id="15")
    t = await _team(db_session, comp_id=c)
    await _log(db_session, comp_id=c, team=t, player=no_birth, pts=10, games=2)
    await _log(db_session, comp_id=c, team=t, player=has_birth, pts=20, games=2)
    # Season rows required for career grain (ticket #405 source switch).
    await _season(db_session, player=no_birth, comp_id=c, year=2023,
                  venue_slug="las_vegas", gp=2, minutes=60.0, pts=20)
    await _season(db_session, player=has_birth, comp_id=c, year=2023,
                  venue_slug="las_vegas", gp=2, minutes=60.0, pts=40)
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", age_min=20),
    )
    assert result.total == 1
    assert result.rows[0].label == "Has Birth"


async def _seed_age_filter_per_comp(
    db: AsyncSession,
) -> None:
    """Seed per_competition age filter data.

    young_player: born 2004, plays in 2023 → per_comp age = 2023 - 2004 = 19
    old_player:   born 1999, plays in 2023 → per_comp age = 2023 - 1999 = 24
    """
    young = make_player("YoungPC", "Rookie")
    young.birthdate = date(2004, 1, 1)

    old = make_player("OldPC", "Vet")
    old.birthdate = date(1999, 1, 1)

    db.add_all([young, old])
    await db.flush()

    c = await _comp(db, year=2023, venue_slug="las_vegas", league_id="15")
    for player, pts in ((young, 20), (old, 15)):
        assert player.id is not None
        db.add(
            SummerLeaguePlayerSeason(
                competition_id=c,
                player_id=player.id,
                year=2023,
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
            )
        )
    await db.flush()
    await db.commit()


@pytest.mark.asyncio
async def test_age_min_filter_per_competition(
    db_session: AsyncSession,
) -> None:
    """age_min filter in per_competition grain: only players old enough are returned.

    young_player (per_comp age 19) excluded by age_min=22; old_player (age 24) included.
    """
    await _seed_age_filter_per_comp(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            age_min=22,
            min_games=1,
            min_minutes=1,
        ),
    )
    assert result.total == 1
    assert result.rows[0].label.startswith("OldPC")


@pytest.mark.asyncio
async def test_age_max_filter_per_competition(
    db_session: AsyncSession,
) -> None:
    """age_max filter in per_competition grain: only players young enough are returned.

    old_player (per_comp age 24) excluded by age_max=21; young_player (age 19) included.
    """
    await _seed_age_filter_per_comp(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            age_max=21,
            min_games=1,
            min_minutes=1,
        ),
    )
    assert result.total == 1
    assert result.rows[0].label.startswith("YoungPC")


@pytest.mark.asyncio
async def test_age_filter_composes_with_venue_filter(
    db_session: AsyncSession,
) -> None:
    """Age filter composes correctly with venue filter (career grain).

    Seeds two competitions for the same player (different venues). The age
    filter should compose with the venue filter without expanding or collapsing
    the result set incorrectly.  With venue=las_vegas AND age_max=21 only the
    young player at that venue should be visible.
    """
    young = make_player("YoungVenue", "Player")
    young.birthdate = date(2004, 1, 1)  # career age at 2023 = 19

    old = make_player("OldVenue", "Player")
    old.birthdate = date(1998, 1, 1)  # career age at 2023 = 25

    db_session.add_all([young, old])
    await db_session.flush()

    c_lv = await _comp(db_session, year=2023, venue_slug="las_vegas", league_id="15")
    c_slc = await _comp(
        db_session, year=2023, venue_slug="salt_lake_city", league_id="16"
    )
    t_lv = await _team(db_session, comp_id=c_lv)
    t_slc = await _team(db_session, comp_id=c_slc)

    # Both players at Las Vegas; only old at Salt Lake.
    await _log(db_session, comp_id=c_lv, team=t_lv, player=young, pts=20, games=2)
    await _log(db_session, comp_id=c_lv, team=t_lv, player=old, pts=15, games=2)
    await _log(db_session, comp_id=c_slc, team=t_slc, player=old, pts=15, games=2)
    # Season rows for career grain (ticket #405 source switch).
    await _season(db_session, player=young, comp_id=c_lv, year=2023,
                  venue_slug="las_vegas", gp=2, minutes=60.0, pts=40)
    await _season(db_session, player=old, comp_id=c_lv, year=2023,
                  venue_slug="las_vegas", gp=2, minutes=60.0, pts=30)
    await _season(db_session, player=old, comp_id=c_slc, year=2023,
                  venue_slug="salt_lake_city", gp=2, minutes=60.0, pts=30)
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="career",
            venue="las_vegas",
            age_max=21,
        ),
    )
    assert result.total == 1
    assert result.rows[0].label == "YoungVenue Player"


@pytest.mark.asyncio
async def test_parse_query_age_min_max_parsed() -> None:
    """parse_query correctly parses age_min and age_max from query string."""
    q = parse_query({"age_min": "19", "age_max": "24"})
    assert q.age_min == 19
    assert q.age_max == 24


@pytest.mark.asyncio
async def test_parse_query_age_invalid_values_become_none() -> None:
    """Invalid / blank age values degrade to None (filter off)."""
    q = parse_query({"age_min": "bad", "age_max": ""})
    assert q.age_min is None
    assert q.age_max is None


# --------------------------------------------------------------------------- #
# Phase 4b: CSV export
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_csv_export_players_status_and_headers(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """?format=csv returns 200, Content-Type: text/csv, Content-Disposition: attachment.

    Seeds the standard two-player fixture and confirms the CSV response has the
    correct HTTP status code and headers for a file download.
    """
    await _seed(db_session)
    resp = await app_client.get("/stats/summer-league/explorer?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    cd = resp.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "summer-league-explorer.csv" in cd


@pytest.mark.asyncio
async def test_csv_export_players_header_row_matches_columns(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """CSV header row starts with 'Player' and contains all Explorer column labels.

    The header must include the leading label column ('Player') followed by every
    stat column label in the players subject (GP, MIN, PTS, etc.).
    """
    await _seed(db_session)
    resp = await app_client.get(
        "/stats/summer-league/explorer?subject=players&format=csv"
    )
    assert resp.status_code == 200
    lines = resp.text.strip().splitlines()
    assert len(lines) >= 1
    header = lines[0]
    assert header.startswith("Player")
    # A sample of expected stat column labels present in the header.
    for label in ("GP", "PTS", "REB", "AST", "FGM", "FGA"):
        assert label in header, f"expected column label {label!r} in CSV header"


@pytest.mark.asyncio
async def test_csv_export_players_data_rows_match_html(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """CSV data rows match what the HTML table would show for the same query.

    Seeds two players (Big Scorer 30 PPG, Role Player 10 PPG). The CSV (sorted
    desc by pts) should list Big Scorer first, Role Player second. The PTS column
    value for Big Scorer must be 30.0 (per_game default).
    """
    await _seed(db_session)
    resp = await app_client.get(
        "/stats/summer-league/explorer?subject=players&sort=pts&dir=desc&format=csv"
    )
    assert resp.status_code == 200
    lines = resp.text.strip().splitlines()
    # lines[0] = header; lines[1] = Big Scorer (highest PTS); lines[2] = Role Player
    assert len(lines) == 3
    assert lines[1].startswith("Big Scorer")
    assert lines[2].startswith("Role Player")
    # PTS value for Big Scorer should be 30.0 in per_game mode.
    header_cols = lines[0].split(",")
    pts_idx = header_cols.index("PTS")
    big_scorer_cols = lines[1].split(",")
    assert big_scorer_cols[pts_idx] == "30.0"


@pytest.mark.asyncio
async def test_csv_export_teams_subject(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """?subject=teams&format=csv returns a valid CSV with 'Team' as the first header.

    Seeds two teams (Alpha/Bravo). The CSV must start with 'Team' as the label
    column and include the teams stat column labels (GP, W, L, etc.).
    """
    await _seed_teams(db_session)
    resp = await app_client.get(
        "/stats/summer-league/explorer?subject=teams&format=csv"
    )
    assert resp.status_code == 200
    lines = resp.text.strip().splitlines()
    assert len(lines) >= 3  # header + 2 team rows
    assert lines[0].startswith("Team")
    for label in ("GP", "W", "L"):
        assert label in lines[0], f"expected {label!r} in teams CSV header"
    # Both team rows should be present.
    team_labels = [line.split(",")[0] for line in lines[1:]]
    assert any("Alpha" in lbl for lbl in team_labels)
    assert any("Bravo" in lbl for lbl in team_labels)


@pytest.mark.asyncio
async def test_csv_export_games_subject(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """?subject=games&format=csv returns a valid CSV with 'Game' as the first header.

    Seeds two games (from _seed_teams). The CSV must start with 'Game', include
    'Total' and 'Margin' columns, and have one data row per scored game.
    """
    await _seed_teams(db_session)
    resp = await app_client.get(
        "/stats/summer-league/explorer?subject=games&format=csv"
    )
    assert resp.status_code == 200
    lines = resp.text.strip().splitlines()
    assert len(lines) == 3  # header + 2 game rows
    assert lines[0].startswith("Game")
    assert "Total" in lines[0]
    assert "Margin" in lines[0]


@pytest.mark.asyncio
async def test_csv_export_none_values_render_as_empty_string(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """None values in stat cells render as empty strings (not 'None') in the CSV.

    Uses per_36 mode where plus_minus is suppressed (None). The corresponding
    CSV cell for plus_minus should be empty, not the literal string 'None'.
    """
    await _seed(db_session)
    resp = await app_client.get(
        "/stats/summer-league/explorer?subject=players&mode=per_36&format=csv"
    )
    assert resp.status_code == 200
    # 'None' must not appear anywhere in the CSV body.
    assert "None" not in resp.text


@pytest.mark.asyncio
async def test_csv_download_link_present_in_html(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """The HTML Explorer page includes a 'Download CSV' link when results are present.

    Confirms that the download link with format=csv is rendered in the results
    section when the query returns at least one row.
    """
    await _seed(db_session)
    resp = await app_client.get("/stats/summer-league/explorer")
    assert resp.status_code == 200
    assert "Download CSV" in resp.text
    assert "format=csv" in resp.text


@pytest.mark.asyncio
async def test_csv_download_link_absent_when_no_results(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """The 'Download CSV' link is absent when the query returns no rows.

    Filters to a venue with no data (min_gp=9999) so the result is empty;
    the download link must not appear.
    """
    await _seed(db_session)
    resp = await app_client.get("/stats/summer-league/explorer?min_gp=9999")
    assert resp.status_code == 200
    assert "Download CSV" not in resp.text


# --------------------------------------------------------------------------- #
# QA fixes #394–#397
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_position_facet_empty_when_no_position_data(
    db_session: AsyncSession,
) -> None:
    """#394: the positions facet is empty when no player carries a position."""
    await _seed(db_session)  # _seed players have no position set
    result = await run_explorer_query(db_session, ExplorerQuery(subject="players"))
    assert result.facets.positions == []


@pytest.mark.asyncio
async def test_position_filter_hidden_when_facet_empty(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """#394: the Position control is omitted from the page when there is no data."""
    await _seed(db_session)
    resp = await app_client.get("/stats/summer-league/explorer")
    assert resp.status_code == 200
    assert 'id="ex-position"' not in resp.text


@pytest.mark.asyncio
async def test_position_filter_shown_when_facet_populated(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """#394: the Position control reappears once position data exists."""
    await _seed_with_positions(db_session)
    resp = await app_client.get("/stats/summer-league/explorer")
    assert resp.status_code == 200
    assert 'id="ex-position"' in resp.text


async def _seed_mixed_countries(db: AsyncSession) -> None:
    """Three USA players stored under three encodings, plus one Australian."""
    p_code = make_player("Code", "Yank")
    p_code.birth_country = "US"
    p_alias = make_player("Alias", "Yank")
    p_alias.birth_country = "USA"
    p_name = make_player("Name", "Yank")
    p_name.birth_country = "United States"
    aussie = make_player("Down", "Under")
    aussie.birth_country = "AU"
    db.add_all([p_code, p_alias, p_name, aussie])
    await db.flush()

    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db, comp_id=c)
    for pl in (p_code, p_alias, p_name, aussie):
        await _log(db, comp_id=c, team=t, player=pl, pts=20, games=2)
        # Season rows required for career grain (ticket #405 source switch).
        await _season(db, player=pl, comp_id=c, year=2024, venue_slug="las_vegas",
                      gp=2, minutes=60.0, pts=40)
    await db.commit()


@pytest.mark.asyncio
async def test_country_facet_has_no_duplicate_encodings(
    db_session: AsyncSession,
) -> None:
    """#395: the country facet lists each country once, normalized and sorted."""
    await _seed_mixed_countries(db_session)
    result = await run_explorer_query(db_session, ExplorerQuery(subject="players"))
    countries = result.facets.countries
    assert countries == sorted(countries)  # alphabetical
    assert len(countries) == len(set(countries))  # no duplicates
    assert "United States" in countries
    assert "Australia" in countries
    # No raw ISO codes or aliases leak through.
    assert not {"US", "USA", "AU"} & set(countries)


@pytest.mark.asyncio
async def test_country_filter_matches_all_encodings(db_session: AsyncSession) -> None:
    """#395: filtering by the canonical name matches every stored encoding."""
    await _seed_mixed_countries(db_session)
    result = await run_explorer_query(
        db_session, ExplorerQuery(subject="players", country="United States")
    )
    # All three USA-encoded players match; the Australian does not.
    assert result.total == 3
    assert {r.label for r in result.rows} == {"Code Yank", "Alias Yank", "Name Yank"}


@pytest.mark.asyncio
async def test_draft_class_facet_clamps_future_years(db_session: AsyncSession) -> None:
    """#396: implausible future draft classes never appear in the facet."""
    future = make_player("Phantom", "Future")
    future.draft_year = date.today().year + 5  # e.g. 2031
    legit = make_player("Real", "Prospect")
    legit.draft_year = 2024
    db_session.add_all([future, legit])
    await db_session.flush()

    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db_session, comp_id=c)
    await _log(db_session, comp_id=c, team=t, player=future, pts=20, games=2)
    await _log(db_session, comp_id=c, team=t, player=legit, pts=15, games=2)
    await db_session.commit()

    result = await run_explorer_query(db_session, ExplorerQuery(subject="players"))
    assert result.facets.draft_classes  # non-empty
    assert max(result.facets.draft_classes) <= date.today().year + 1
    assert (date.today().year + 5) not in result.facets.draft_classes


async def _seed_uneven_gp(db: AsyncSession) -> None:
    """Two players in one pool whose per-game and total rankings disagree.

    High-rate plays 2 games at 30 PTS (60 total, 30.0/g); Grinder plays 5 games
    at 25 PTS (125 total, 25.0/g). Sorting on totals would rank Grinder first
    despite a lower per-game average — the #397 bug.
    Season rows match the game-log totals for career grain (ticket #405 source switch).
    """
    high_rate = make_player("High", "Rate")
    grinder = make_player("Grind", "Er")
    db.add_all([high_rate, grinder])
    await db.flush()
    c = await _comp(db, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db, comp_id=c)
    await _log(db, comp_id=c, team=t, player=high_rate, pts=30, games=2)
    await _log(db, comp_id=c, team=t, player=grinder, pts=25, games=5)
    # Season rows: gp/minutes/pts match the game-log sums exactly.
    await _season(db, player=high_rate, comp_id=c, year=2024, venue_slug="las_vegas",
                  gp=2, minutes=60.0, pts=60)
    await _season(db, player=grinder, comp_id=c, year=2024, venue_slug="las_vegas",
                  gp=5, minutes=150.0, pts=125)
    await db.commit()


@pytest.mark.asyncio
async def test_career_per_game_sort_is_monotonic(db_session: AsyncSession) -> None:
    """#397: a per-game career sort ranks on the displayed rate, not the total."""
    await _seed_uneven_gp(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", mode="per_game", sort="pts"),
    )
    pts_col = [r.values["pts"] for r in result.rows]
    assert pts_col == sorted(pts_col, reverse=True)  # visually monotonic
    assert pts_col == [30.0, 25.0]
    assert result.rows[0].label == "High Rate"


@pytest.mark.asyncio
async def test_career_totals_sort_still_ranks_by_total(
    db_session: AsyncSession,
) -> None:
    """#397: totals mode still ranks by the season total (Grinder's 125 > 60)."""
    await _seed_uneven_gp(db_session)
    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", mode="totals", sort="pts"),
    )
    assert result.rows[0].label == "Grind Er"
    assert result.rows[0].values["pts"] == 125


@pytest.mark.asyncio
async def test_per_competition_per_game_sort_is_monotonic(
    db_session: AsyncSession,
) -> None:
    """#397: per_competition per-game sort is monotonic on the displayed rate.

    Two season rows in one pool whose per-game and total orderings disagree:
    High-rate (2 GP, 60 PTS → 30.0/g) vs Grinder (5 GP, 125 PTS → 25.0/g).
    Sorting on the season total would invert the displayed column.
    """
    high_rate = make_player("High", "Rate")
    grinder = make_player("Grind", "Er")
    db_session.add_all([high_rate, grinder])
    await db_session.flush()
    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    await _season(
        db_session, player=high_rate, comp_id=c, year=2024, venue_slug="las_vegas",
        gp=2, minutes=60.0, pts=60,
    )
    await _season(
        db_session, player=grinder, comp_id=c, year=2024, venue_slug="las_vegas",
        gp=5, minutes=150.0, pts=125,
    )
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players",
            grain="per_competition",
            mode="per_game",
            sort="pts",
            year_min=2024,
            year_max=2024,
            venue="las_vegas",
        ),
    )
    pts_col = [r.values["pts"] for r in result.rows]
    assert pts_col == sorted(pts_col, reverse=True)
    assert pts_col == [30.0, 25.0]
    # per_competition labels carry a "· <competition>" suffix.
    assert result.rows[0].label.startswith("High Rate")


# --------------------------------------------------------------------------- #
# Ticket #405: season-table source switch for career grain
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_career_box_parity_after_source_switch(
    db_session: AsyncSession,
) -> None:
    """Career grain sources box totals from the season table, not game-log sums.

    Seeds a player with 3 game logs (summing to pts=15, reb=9, ast=6, fgm=6, fga=15)
    AND a season row whose box totals are deliberately DIFFERENT (pts=20, reb=11, …).
    The explorer career grain now reads summer_league_player_seasons, so it must
    return the SEASON values, not the game-log sums. Choosing divergent values makes
    this test genuinely guard the source switch: it would fail if the career grain
    reverted to summing game logs. (Materialization correctness — that a season row's
    totals equal its logs — is the rebuild job's concern, not the explorer's.)
    """
    player = make_player("Parity", "Player")
    db_session.add(player)
    await db_session.flush()

    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db_session, comp_id=c)

    # Seed 3 game logs: each has pts=5, reb=3, ast=2, fgm=2, fga=5, 30 min.
    for _ in range(3):
        _N["i"] += 1
        g = SummerLeagueGame(
            competition_id=c,
            nba_stats_game_id=f"parity-game-{_N['i']}",
            game_date=date(2024, 7, 10),
            home_team_entry_id=t.id,
            away_team_entry_id=t.id,
            home_score=100,
            away_score=90,
        )
        db_session.add(g)
        await db_session.flush()
        assert g.id is not None
        sp = SummerLeagueSourcePlayer(
            nba_stats_person_id=f"parity-sp-{_N['i']}",
            raw_player_name=player.display_name or "Player",
            normalized_name=(player.display_name or "player").lower(),
            canonical_player_id=player.id,
        )
        db_session.add(sp)
        await db_session.flush()
        db_session.add(
            SummerLeaguePlayerGameLog(
                competition_id=c,
                game_id=g.id,
                team_entry_id=t.id,
                source_player_id=sp.id,
                player_id=player.id,
                nba_stats_person_id=sp.nba_stats_person_id,
                raw_player_name=player.display_name or "Player",
                minutes_seconds=1800,  # 30 min
                pts=5,
                reb=3,
                ast=2,
                fgm=2,
                fga=5,
            )
        )
    await db_session.flush()

    # Season row: box totals deliberately DIFFERENT from the game-log sums above
    # (logs sum to pts=15/reb=9/ast=6/fgm=6/fga=15). The explorer must surface these
    # season values, proving it reads the season table rather than the game logs.
    assert player.id is not None
    db_session.add(
        SummerLeaguePlayerSeason(
            competition_id=c,
            player_id=player.id,
            year=2024,
            venue_slug="las_vegas",
            gp=3,
            minutes=95.0,    # ≠ 90 (3 × 30 min log sum)
            pts=20,          # ≠ 15 (log sum)
            reb=11,          # ≠ 9
            ast=7,           # ≠ 6
            fgm=8,           # ≠ 6
            fga=18,          # ≠ 15
            fg3m=0,
            fg3a=0,
            ftm=0,
            fta=0,
            oreb=0,
            dreb=11,
            blk=0,
            stl=0,
            tov=0,
            pf=0,
            plus_minus=0,
        )
    )
    await db_session.flush()
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", mode="totals", min_games=1),
    )
    assert result.total == 1
    row = result.rows[0]
    # Career grain must return the SEASON-table totals, not the game-log sums.
    assert row.values["gp"] == 3
    assert row.values["pts"] == 20   # season value, not the log sum (15)
    assert row.values["reb"] == 11
    assert row.values["ast"] == 7
    assert row.values["fgm"] == 8
    assert row.values["fga"] == 18
    # Explicit guard against a revert to game-log sourcing:
    assert row.values["pts"] != 15
    assert row.values["fga"] != 15
    # Per-game mode cross-check: 20 pts / 3 gp = 6.7 (1-decimal)
    result_pg = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", mode="per_game", min_games=1),
    )
    assert result_pg.rows[0].values["pts"] == 6.7
    # FG% in totals mode: fgm/fga = 8/18 = 44.4%
    assert row.values["fg_pct"] == 44.4


@pytest.mark.asyncio
async def test_career_additive_sum(
    db_session: AsyncSession,
) -> None:
    """Career WS and VORP equal the sum of per-competition season rows.

    Seeds two competitions for one player, each with WS/VORP values.
    The career grain must sum them (additive bucket), not average.
    """
    player = make_player("Additive", "Star")
    db_session.add(player)
    await db_session.flush()

    c1 = await _comp(db_session, year=2023, venue_slug="las_vegas", league_id="15")
    c2 = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")

    # Competition 1: WS=0.8, VORP=0.3
    await _season(db_session, player=player, comp_id=c1, year=2023,
                  venue_slug="las_vegas", gp=3, minutes=90.0, pts=45,
                  ws=0.8, vorp=0.3, adv_eligible=True)
    # Competition 2: WS=1.2, VORP=0.5
    await _season(db_session, player=player, comp_id=c2, year=2024,
                  venue_slug="las_vegas", gp=4, minutes=120.0, pts=60,
                  ws=1.2, vorp=0.5, adv_eligible=True)
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", mode="totals", min_games=1),
    )
    assert result.total == 1
    row = result.rows[0]
    # Additive: career WS = 0.8 + 1.2 = 2.0; VORP = 0.3 + 0.5 = 0.8
    assert row.values["ws"] == pytest.approx(2.0, abs=0.05)
    assert row.values["vorp"] == pytest.approx(0.8, abs=0.05)
    # GP and PTS should also be summed additive totals.
    assert row.values["gp"] == 7   # 3 + 4
    assert row.values["pts"] == 105  # 45 + 60


@pytest.mark.asyncio
async def test_career_rate_composite_minute_weighted_across_distinct_pools(
    db_session: AsyncSession,
) -> None:
    """Career PER is the minute-weighted mean of distinct per-competition pools.

    Guards the *production* SQL roll-up ``_rate_composite_agg`` (not the standalone
    ``rollup_rate_composite`` helper). Two competitions with DISTINCT PER and DISTINCT
    minutes are seeded so that minute-weighted-avg, simple-mean, and sum all differ —
    making the assertion fail loudly if the SQL aggregate is inverted (e.g. swapped to
    a plain SUM or an unweighted AVG) or if minute-weighting is dropped.

    Pool 1: PER 20 @ 100 min; Pool 2: PER 10 @ 200 min.
      minute-weighted = (20*100 + 10*200) / (100+200) = 4000/300 ≈ 13.33
      simple mean      = 15.00
      sum              = 30.00
    """
    player = make_player("Weighted", "Composite")
    db_session.add(player)
    await db_session.flush()

    c1 = await _comp(db_session, year=2023, venue_slug="las_vegas", league_id="15")
    c2 = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")

    await _season_with_composites(
        db_session, player=player, comp_id=c1, year=2023, venue_slug="las_vegas",
        gp=3, minutes=100.0, per=20.0, adv_eligible=True,
    )
    await _season_with_composites(
        db_session, player=player, comp_id=c2, year=2024, venue_slug="las_vegas",
        gp=3, minutes=200.0, per=10.0, adv_eligible=True,
    )
    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", min_games=1, min_minutes=1),
    )
    assert result.total == 1
    per_value = result.rows[0].values["per"]
    assert per_value is not None

    # Cross-check the SQL result against the standalone roll-up primitive on the same
    # data so the two implementations cannot drift apart silently.
    from types import SimpleNamespace

    pools = [
        SimpleNamespace(per=20.0, minutes=100.0),
        SimpleNamespace(per=10.0, minutes=200.0),
    ]
    expected = rollup_rate_composite(pools, "per")
    assert expected == pytest.approx(13.333, abs=0.01)

    # Minute-weighted mean — NOT the simple mean (15.0) and NOT the sum (30.0).
    assert per_value == pytest.approx(expected, abs=0.05)
    assert per_value != pytest.approx(15.0, abs=0.05)
    assert per_value != pytest.approx(30.0, abs=0.05)


@pytest.mark.asyncio
async def test_per_game_still_reads_game_logs(
    db_session: AsyncSession,
) -> None:
    """per_game grain continues to read SummerLeaguePlayerGameLog, not season rows.

    Seeds a player with game logs but NO season rows. The per_game grain must
    still return results (proving it reads game logs, not the season table).
    Seeds a second player with a season row but NO game logs; that player must
    NOT appear in the per_game result (confirming per_game ignores season rows).
    """
    # Player A: game logs only (no season row).
    player_a = make_player("LogOnly", "Player")
    db_session.add(player_a)
    await db_session.flush()

    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    t = await _team(db_session, comp_id=c)
    await _log(db_session, comp_id=c, team=t, player=player_a, pts=20, games=1)

    # Player B: season row only (no game logs).
    player_b = make_player("SeasonOnly", "Player")
    db_session.add(player_b)
    await db_session.flush()
    await _season(db_session, player=player_b, comp_id=c, year=2024,
                  venue_slug="las_vegas", gp=2, minutes=60.0, pts=30)

    await db_session.commit()

    result = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="per_game", min_games=1),
    )
    labels = {r.label.split(" · ")[0] for r in result.rows}
    # LogOnly player appears (has game logs); SeasonOnly does not (no game logs).
    assert "LogOnly Player" in labels
    assert "SeasonOnly Player" not in labels


# --------------------------------------------------------------------------- #
# Ticket #406: advanced columns at all grains, catalog-driven sort, pooled marker
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_advanced_columns_present_and_sortable_all_grains(
    db_session: AsyncSession,
) -> None:
    """#406: Advanced columns appear in result.columns for career and per_competition grains.

    Also verifies that sorting by advanced keys (per, bpm, ws, vorp, ts_pct)
    is valid at both grains and does not coerce to the default sort key.
    Per_game grain must NOT surface advanced composite columns in its column list.
    """
    player = make_player("Adv", "Star")
    db_session.add(player)
    await db_session.flush()

    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    await _metric_context(db_session, comp_id=c, year=2024, venue_slug="las_vegas",
                          adv_eligible=True)
    await _season_with_composites(
        db_session, player=player, comp_id=c, year=2024, venue_slug="las_vegas",
        per=20.0, bpm=2.5, ws=1.0, vorp=0.5,
    )
    t = await _team(db_session, comp_id=c)
    await _log(db_session, comp_id=c, team=t, player=player, pts=20, games=3)
    await db_session.commit()

    adv_keys = {c.key for c in _PLAYER_ADVANCED_COLUMNS}

    # Career grain: advanced columns always present.
    career = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", min_games=1, min_minutes=1),
    )
    career_col_keys = {c.key for c in career.columns}
    assert adv_keys <= career_col_keys, f"missing at career: {adv_keys - career_col_keys}"
    # Row values populated for advanced keys.
    assert career.rows[0].values.get("ws") is not None
    assert career.rows[0].values.get("per") is not None

    # Career grain: sorting by advanced keys accepted (no coercion to default).
    for sort_key in ("per", "bpm", "ws", "vorp", "ts_pct"):
        q_sort = parse_query({"grain": "career", "sort": sort_key})
        assert q_sort.sort == sort_key, (
            f"sort={sort_key!r} at career coerced to {q_sort.sort!r}"
        )

    # Per_competition multi-comp: advanced columns also present.
    pc_multi = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players", grain="per_competition", min_games=1, min_minutes=1
        ),
    )
    pc_col_keys = {c.key for c in pc_multi.columns}
    assert adv_keys <= pc_col_keys, f"missing at per_competition: {adv_keys - pc_col_keys}"

    # Per_game grain: advanced composite columns NOT in result.columns.
    pg = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="per_game"),
    )
    pg_col_keys = {c.key for c in pg.columns}
    composite_adv_keys = {k for k in adv_keys if k != "ts_pct"}
    assert pg_col_keys.isdisjoint(composite_adv_keys), (
        f"per_game should not have composite adv columns: {pg_col_keys & composite_adv_keys}"
    )


@pytest.mark.asyncio
async def test_pooled_avg_marker_and_eligibility_banner(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """#406: Career grain rows carry pooled_composite_keys; HTML shows 'avg' marker and N-of-M banner.

    Verifies:
    - result.pooled_composite_keys contains rate_composite column keys (per, ortg, drtg, bpm)
      at career grain.
    - result.pooled_composite_keys is empty at per_competition grain (each row is one pool).
    - HTML response for career grain includes the 'avg' marker text and the N-of-M banner.
    """
    player = make_player("Pool", "Tester")
    db_session.add(player)
    await db_session.flush()

    c = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    await _metric_context(db_session, comp_id=c, year=2024, venue_slug="las_vegas",
                          adv_eligible=True)
    await _season_with_composites(
        db_session, player=player, comp_id=c, year=2024, venue_slug="las_vegas",
        per=18.0, bpm=1.5, ws=0.9, vorp=0.4,
    )
    await db_session.commit()

    # Career grain: pooled_composite_keys must contain rate_composite keys.
    career = await run_explorer_query(
        db_session,
        ExplorerQuery(subject="players", grain="career", min_games=1, min_minutes=1),
    )
    assert "per" in career.pooled_composite_keys
    assert "bpm" in career.pooled_composite_keys
    assert "ortg" in career.pooled_composite_keys
    assert "drtg" in career.pooled_composite_keys
    # Additive columns are exact (not pooled).
    assert "ws" not in career.pooled_composite_keys
    assert "vorp" not in career.pooled_composite_keys
    # Recombinable (ts_pct) is exact.
    assert "ts_pct" not in career.pooled_composite_keys
    # adv_eligible_n/m populated.
    assert career.adv_eligible_m > 0
    assert career.adv_eligible_n >= 0

    # Per_competition grain: pooled_composite_keys is empty.
    pc = await run_explorer_query(
        db_session,
        ExplorerQuery(
            subject="players", grain="per_competition", min_games=1, min_minutes=1
        ),
    )
    assert len(pc.pooled_composite_keys) == 0

    # HTML: career grain page shows "avg" marker text and N-of-M banner.
    resp = await app_client.get(
        "/stats/summer-league/explorer?grain=career&min_gp=1&min_min=1"
    )
    assert resp.status_code == 200
    body = resp.text
    # N-of-M banner is present (the count text includes "of").
    assert "slg-explorer-banner" in body
    assert " of " in body  # N of M text
    # "avg" marker is present for pooled composites.
    assert "slg-pooled-mark" in body
    # #429: the banner message (incl. the inline "avg" abbr) lives in a single
    # flex item so it flows as normal text. Guard: the <abbr> marker sits INSIDE
    # the slg-explorer-banner__text wrapper, not as a bare flex sibling.
    assert "slg-explorer-banner__text" in body
    _i = body.index('slg-explorer-banner__text"')
    _text_span = body[_i : body.index("</span>", _i)]
    assert "<abbr" in _text_span, "avg marker must be inside the banner text wrapper"


@pytest.mark.asyncio
async def test_invalid_sort_coerces(db_session: AsyncSession) -> None:
    """#406: Invalid or inapplicable sort keys coerce to the default without 500.

    - ?sort=bogus → coerces to 'pts' (not a valid key at all).
    - ?sort=per&grain=per_game → coerces to 'pts' (per not in per_game SELECT).
    - ?sort=per&grain=career → stays 'per' (valid at career).
    - ?sort=ts_pct&grain=per_game → stays 'ts_pct' (ts_pct is box-derived, valid).
    """
    # Pure parse_query checks (no DB needed).
    q_bogus = parse_query({"sort": "bogus"})
    assert q_bogus.sort == "pts", f"expected 'pts', got {q_bogus.sort!r}"

    q_per_pg = parse_query({"sort": "per", "grain": "per_game"})
    assert q_per_pg.sort == "pts", f"expected 'pts' for per at per_game, got {q_per_pg.sort!r}"

    q_per_career = parse_query({"sort": "per", "grain": "career"})
    assert q_per_career.sort == "per", (
        f"expected 'per' at career grain, got {q_per_career.sort!r}"
    )

    q_ts_pg = parse_query({"sort": "ts_pct", "grain": "per_game"})
    assert q_ts_pg.sort == "ts_pct", (
        f"ts_pct should be valid at per_game (box-derived), got {q_ts_pg.sort!r}"
    )

    q_bpm_career = parse_query({"sort": "bpm", "grain": "career"})
    assert q_bpm_career.sort == "bpm", (
        f"bpm should be valid at career grain, got {q_bpm_career.sort!r}"
    )

    q_ws_pc = parse_query({"sort": "ws", "grain": "per_competition"})
    assert q_ws_pc.sort == "ws", (
        f"ws should be valid at per_competition grain, got {q_ws_pc.sort!r}"
    )


# --------------------------------------------------------------------------- #
# Ticket #407: Metric threshold filters
# --------------------------------------------------------------------------- #

from app.services.summer_league_explorer_service import (  # noqa: E402
    MetricFilter,
    parse_metric_filters,
)


@pytest.mark.asyncio
async def test_metric_threshold_filter(db_session: AsyncSession) -> None:
    """Metric filter on career pts narrows results: pts >= 40 keeps high scorers only.

    Scorer: 60 career pts (2 x 30). Role: 20 career pts (2 x 10).
    fcol0=pts, fop0=gte, fval0=40 → Scorer only.
    fcol0=pts, fop0=lte, fval0=25 → Role only.
    """
    await _seed(db_session)

    q_high = parse_query({"fcol0": "pts", "fop0": "gte", "fval0": "40"})
    result_high = await run_explorer_query(db_session, q_high)
    assert result_high.total == 1
    assert result_high.rows[0].label == "Big Scorer"

    q_low = parse_query({"fcol0": "pts", "fop0": "lte", "fval0": "25"})
    result_low = await run_explorer_query(db_session, q_low)
    assert result_low.total == 1
    assert result_low.rows[0].label == "Role Player"


@pytest.mark.asyncio
async def test_metric_filter_with_facets(db_session: AsyncSession) -> None:
    """Metric filter combines with existing facet filters (venue).

    Both players would pass pts >= 5 alone. Combining with venue=las_vegas
    (Scorer only) should return only Scorer.
    """
    await _seed(db_session)

    q = parse_query({"fcol0": "pts", "fop0": "gte", "fval0": "5", "venue": "las_vegas"})
    result = await run_explorer_query(db_session, q)
    assert result.total == 1
    assert result.rows[0].label == "Big Scorer"


@pytest.mark.asyncio
async def test_metric_filter_invalid_inputs_graceful(
    db_session: AsyncSession, app_client: AsyncClient
) -> None:
    """Invalid metric filter inputs are silently dropped — never a 500.

    Tests: unknown column, unknown operator, non-numeric value, non-filterable column.
    Valid filters in the same request still apply (or none if all invalid).
    """
    await _seed(db_session)

    # Unknown column: ignored, all 2 players returned.
    r1 = await app_client.get(
        "/stats/summer-league/explorer?fcol0=nonexistent_col&fop0=gte&fval0=10"
    )
    assert r1.status_code == 200
    assert "2 results" in r1.text

    # Non-filterable column (fgm is box but not filterable).
    r2 = await app_client.get(
        "/stats/summer-league/explorer?fcol0=fgm&fop0=gte&fval0=5"
    )
    assert r2.status_code == 200
    assert "2 results" in r2.text

    # Bad operator: ignored.
    r3 = await app_client.get(
        "/stats/summer-league/explorer?fcol0=pts&fop0=INVALID&fval0=10"
    )
    assert r3.status_code == 200
    assert "2 results" in r3.text

    # Non-numeric value: ignored.
    r4 = await app_client.get(
        "/stats/summer-league/explorer?fcol0=pts&fop0=gte&fval0=not_a_number"
    )
    assert r4.status_code == 200
    assert "2 results" in r4.text

    # Mixed: one invalid + one valid (pts >= 40 keeps Scorer only).
    r5 = await app_client.get(
        "/stats/summer-league/explorer"
        "?fcol0=BADCOL&fop0=gte&fval0=99"
        "&fcol1=pts&fop1=gte&fval1=40"
    )
    assert r5.status_code == 200
    assert "1 result" in r5.text
    assert "Big Scorer" in r5.text


@pytest.mark.asyncio
async def test_advanced_url_roundtrip(db_session: AsyncSession) -> None:
    """Metric filters round-trip through parse_query and produce correct DB results.

    parse_query correctly parses fcol/fop/fval params into MetricFilter objects.
    The same params produce the same result as running ExplorerQuery directly.
    """
    # parse_metric_filters unit checks.
    filters = parse_metric_filters({
        "fcol0": "pts", "fop0": "gte", "fval0": "20",
        "fcol1": "per", "fop1": "lte", "fval1": "15.5",
        "fcol2": "efg_pct", "fop2": "gte", "fval2": "50",
    })
    assert len(filters) == 3
    assert filters[0] == MetricFilter(col="pts", op=">=", value=20.0)
    assert filters[1] == MetricFilter(col="per", op="<=", value=15.5)
    assert filters[2] == MetricFilter(col="efg_pct", op=">=", value=50.0)

    # Round-trip: URL params → parse_query → same filters.
    q = parse_query({
        "fcol0": "pts", "fop0": "gte", "fval0": "40",
    })
    assert len(q.metric_filters) == 1
    assert q.metric_filters[0] == MetricFilter(col="pts", op=">=", value=40.0)

    # DB result: pts >= 40 (career totals) → Scorer (60 pts) passes, Role (20 pts) fails.
    await _seed(db_session)
    result = await run_explorer_query(db_session, q)
    assert result.total == 1
    assert result.rows[0].label == "Big Scorer"

    # Both valid filters: pts >= 5 (both qualify) and gp >= 2 (both qualify).
    q2 = parse_query({
        "fcol0": "pts", "fop0": "gte", "fval0": "5",
        "fcol1": "gp", "fop1": "gte", "fval1": "2",
    })
    result2 = await run_explorer_query(db_session, q2)
    assert result2.total == 2  # both players qualify


# --------------------------------------------------------------------------- #
# Ticket #409: CSV advanced columns + per-pool drill-down
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_csv_export_advanced(
    db_session: AsyncSession,
    app_client: AsyncClient,
) -> None:
    """CSV export for career grain includes advanced column headers and all rows.

    Career grain always returns _PLAYER_STAT_COLUMNS + _PLAYER_ADVANCED_COLUMNS
    in result.columns; the CSV writer iterates result.columns so the header and
    data rows automatically include PER, BPM, TS%, WS, VORP.
    """
    await _seed(db_session)  # 2 qualifying players

    resp = await app_client.get("/stats/summer-league/explorer?format=csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")

    lines = [ln for ln in resp.text.splitlines() if ln.strip()]
    header = lines[0]

    # Advanced column labels must appear in the header.
    for label in ("TS%", "PER", "ORtg", "DRtg", "BPM", "WS", "VORP"):
        assert label in header, f"expected {label!r} in CSV header, got: {header}"

    # Row count: header + 2 players.
    assert len(lines) == 3, f"expected 3 lines (header + 2 rows), got {len(lines)}"


@pytest.mark.asyncio
async def test_pool_drilldown(
    db_session: AsyncSession,
    app_client: AsyncClient,
) -> None:
    """Drill-down endpoint returns per-competition rows composing a career row.

    Seeds one player who appeared in two competitions (Vegas 2024 and SLC 2025).
    The drill-down response must contain one row per competition, rendered as
    slg-drilldown-row elements, and reference both years in the row labels.
    """
    player = make_player("Drill", "Downer")
    db_session.add(player)
    await db_session.flush()

    c1 = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    c2 = await _comp(db_session, year=2025, venue_slug="salt_lake_city", league_id="16")

    await _season(
        db_session,
        player=player,
        comp_id=c1,
        year=2024,
        venue_slug="las_vegas",
        gp=2,
        minutes=60.0,
        pts=20,
    )
    await _season(
        db_session,
        player=player,
        comp_id=c2,
        year=2025,
        venue_slug="salt_lake_city",
        gp=2,
        minutes=60.0,
        pts=30,
    )
    await db_session.commit()

    assert player.slug is not None
    resp = await app_client.get(
        f"/stats/summer-league/explorer/drilldown?player_slug={player.slug}"
    )
    assert resp.status_code == 200
    body = resp.text

    # Both competition years must appear in the rendered rows.
    assert "2024" in body, "expected 2024 competition row in drilldown"
    assert "2025" in body, "expected 2025 competition row in drilldown"
    # Drilldown rows carry the identifying CSS class.
    assert "slg-drilldown-row" in body

    # Missing player_slug returns 400.
    bad = await app_client.get("/stats/summer-league/explorer/drilldown")
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_pool_drilldown_respects_team_scope(
    db_session: AsyncSession,
    app_client: AsyncClient,
) -> None:
    """Drill-down honors the parent row's team_slug scope.

    A career row filtered by team_slug aggregates only that team's competitions,
    so its breakdown must too. Seeds one player on team A (Vegas 2024) and team B
    (SLC 2025); a drilldown scoped to team A must show only the 2024 row, not 2025.
    Regression for the drill-down dropping team_slug from its scope query.
    """
    player = make_player("Scoped", "Driller")
    db_session.add(player)
    await db_session.flush()

    c1 = await _comp(db_session, year=2024, venue_slug="las_vegas", league_id="15")
    c2 = await _comp(db_session, year=2025, venue_slug="salt_lake_city", league_id="16")
    team_a = await _team(db_session, comp_id=c1)
    team_b = await _team(db_session, comp_id=c2)

    await _season(
        db_session,
        player=player,
        comp_id=c1,
        year=2024,
        venue_slug="las_vegas",
        primary_team_entry_id=team_a.id,
    )
    await _season(
        db_session,
        player=player,
        comp_id=c2,
        year=2025,
        venue_slug="salt_lake_city",
        primary_team_entry_id=team_b.id,
    )
    await db_session.commit()

    assert player.slug is not None
    resp = await app_client.get(
        "/stats/summer-league/explorer/drilldown"
        f"?player_slug={player.slug}&team_slug={team_a.team_slug}"
    )
    assert resp.status_code == 200
    body = resp.text
    assert "2024" in body, "team A's 2024 competition must appear in the breakdown"
    assert "2025" not in body, (
        "team B's 2025 competition must NOT appear when scoped to team A"
    )
