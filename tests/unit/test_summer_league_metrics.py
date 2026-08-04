"""Unit tests for the Summer League advanced-metrics math.

Covers the pure computation: Game Score, the safe-divide guard, the SL
Pythagorean fit, the BPM offense/defense split invariant, and the advanced-metric
gating that blanks league-relative stats for ineligible pools.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.services.sources.summer_league import metrics as metrics_service
from app.services.sources.summer_league.metrics import (
    BPM_FEATURES,
    Box,
    LeagueContext,
    PlayerSeason,
    _d,
    apply_sl_bpm,
    ast_pct_line,
    compute_metrics,
    fit_pythagorean,
    ftr_line,
    game_score,
    game_score_from_row,
    game_score_line,
    tov_pct_line,
)


def _box(**kw: float) -> Box:
    b = Box()
    for k, v in kw.items():
        setattr(b, k, v)
    return b


class _FakeResult:
    """Minimal async-result stand-in for the metrics loader unit test."""

    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self) -> "_FakeResult":
        return self

    def __iter__(self):
        return iter(self.rows)

    def all(self) -> list[object]:
        return self.rows


class _FakeSession:
    """Returns staged results for the five queries issued by ``_load``."""

    def __init__(self, results: list[_FakeResult]) -> None:
        self.results = iter(results)
        self.statements: list[object] = []

    async def execute(self, _statement: object) -> _FakeResult:
        self.statements.append(_statement)
        return next(self.results)


@pytest.mark.asyncio
async def test_load_selects_nba_source_rate_aggregates() -> None:
    """The materializer requests minute-weighted NBA Advanced source rates."""
    db = _FakeSession(
        [
            _FakeResult([SimpleNamespace(id=1, year=2026, venue_slug="las_vegas")]),
            _FakeResult([]),
            _FakeResult([]),
            _FakeResult([(2, 60)]),
            _FakeResult([]),
        ]
    )

    (
        comps,
        games,
        team_rows,
        team_minutes,
        player_rows,
        competition_effective_days,
    ) = await metrics_service._load(db)  # type: ignore[arg-type]

    assert comps == {1: (2026, "las_vegas")}
    assert games == {}
    assert team_rows == []
    assert team_minutes == {2: 1.0}
    assert player_rows == []
    assert competition_effective_days == {}


@pytest.mark.asyncio
async def test_load_through_day_scopes_every_game_backed_query() -> None:
    """Historical builds constrain games, team rows, minutes, and player rows."""
    db = _FakeSession(
        [
            _FakeResult([SimpleNamespace(id=1, year=2019, venue_slug="las_vegas")]),
            _FakeResult([]),
            _FakeResult([]),
            _FakeResult([]),
            _FakeResult([]),
        ]
    )

    await metrics_service._load(  # type: ignore[arg-type]
        db,
        through_day=date(2019, 7, 9),
    )

    rendered = [str(statement) for statement in db.statements]
    assert len(rendered) == 5
    assert "game_date" not in rendered[0]
    assert all("game_date" in statement for statement in rendered[1:])


@pytest.mark.asyncio
async def test_compute_persists_minute_weighted_nba_source_rates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NBA Advanced rates override incomplete team-box fallback calculations."""
    raw_row = SimpleNamespace(
        competition_id=1,
        player_id=7,
        team_entry_id=2,
        gp=3,
        sec=1800,
        plus_minus=0,
        usg_pct_weighted=621.0,
        usg_pct_seconds=1800,
        ast_pct_weighted=270.0,
        ast_pct_seconds=1800,
        trb_pct_weighted=225.0,
        trb_pct_seconds=1800,
        **{field: 0 for field in metrics_service._BOX_INT_FIELDS},
    )
    raw_row.pts = 20
    raw_row.fgm = 8
    raw_row.fga = 15
    raw_row.fta = 4
    raw_row.ftm = 3
    raw_row.fg3m = 2
    raw_row.fg3a = 5
    raw_row.reb = 7
    raw_row.ast = 4
    raw_row.tov = 3

    async def fake_load(_db: object) -> tuple[object, ...]:
        return ({1: (2026, "las_vegas")}, {}, [], {}, [raw_row], {})

    async def empty_dict(_db: object) -> dict[object, object]:
        return {}

    async def no_source_as_of(_db: object) -> None:
        return None

    context = LeagueContext(
        competition_id=1,
        year=2026,
        venue="las_vegas",
        lg=Box(),
        poss=0.0,
        team_games=0,
        adv_eligible=False,
    )
    context.pace = 100.0
    monkeypatch.setattr(metrics_service, "_load", fake_load)
    monkeypatch.setattr(metrics_service, "_source_as_of", no_source_as_of)
    monkeypatch.setattr(metrics_service, "_load_shot_diet", empty_dict)
    monkeypatch.setattr(metrics_service, "_load_assisted_fg", empty_dict)
    monkeypatch.setattr(
        metrics_service,
        "_build",
        lambda *_args: ({2: _box(mp=1000)}, {2: _box(mp=1000)}, {}, {1: context}, {}),
    )

    result = await metrics_service.compute(object())  # type: ignore[arg-type]

    season = result.seasons[0]
    assert season.source_rates == {"usg_pct": 34.5, "ast_pct": 15.0, "trb_pct": 12.5}
    assert season.metrics["usg_pct"] == 34.5
    assert season.metrics["ast_pct"] == 15.0
    assert season.metrics["trb_pct"] == 12.5


def test_safe_divide_guards_zero() -> None:
    """``_d`` returns 0.0 rather than raising on a zero denominator."""
    assert _d(10.0, 2.0) == 5.0
    assert _d(10.0, 0.0) == 0.0


def test_game_score_matches_hollinger_formula() -> None:
    """Game Score equals the Hollinger weighting of a known box line."""
    b = _box(
        pts=20,
        fgm=8,
        fga=15,
        fta=4,
        ftm=3,
        oreb=2,
        dreb=5,
        stl=2,
        ast=4,
        blk=1,
        pf=3,
        tov=2,
    )
    # 20 +0.4*8 -0.7*15 -0.4*(4-3) +0.7*2 +0.3*5 +2 +0.7*4 +0.7*1 -0.4*3 -2
    expected = (
        20
        + 0.4 * 8
        - 0.7 * 15
        - 0.4 * 1
        + 0.7 * 2
        + 0.3 * 5
        + 2
        + 0.7 * 4
        + 0.7 * 1
        - 0.4 * 3
        - 2
    )
    assert game_score(b) == round(expected, 6)


def test_game_score_line_matches_box_and_coalesces_none() -> None:
    """game_score_line equals game_score(Box) and treats missing/None components as 0."""
    b = _box(
        pts=20,
        fgm=8,
        fga=15,
        fta=4,
        ftm=3,
        oreb=2,
        dreb=5,
        stl=2,
        ast=4,
        blk=1,
        pf=3,
        tov=2,
    )
    assert game_score_line(
        pts=20,
        fgm=8,
        fga=15,
        ftm=3,
        fta=4,
        oreb=2,
        dreb=5,
        ast=4,
        stl=2,
        blk=1,
        tov=2,
        pf=3,
    ) == game_score(b)
    # None components coalesce to 0 (here: only points score).
    assert (
        game_score_line(
            pts=10,
            fgm=None,
            fga=None,
            ftm=None,
            fta=None,
            oreb=None,
            dreb=None,
            ast=None,
            stl=None,
            blk=None,
            tov=None,
            pf=None,
        )
        == 10.0
    )


def test_game_score_from_row_handles_objects_and_mappings() -> None:
    """game_score_from_row reads box fields from either an object or a mapping.

    Both forms (an attribute-bearing object and a dict of summed totals) must agree
    with game_score_line, and absent fields coalesce to 0.
    """
    from types import SimpleNamespace

    fields = dict(
        pts=20,
        fgm=8,
        fga=15,
        ftm=3,
        fta=4,
        oreb=2,
        dreb=5,
        ast=4,
        stl=2,
        blk=1,
        tov=2,
        pf=3,
    )
    expected = game_score_line(**fields)
    assert game_score_from_row(SimpleNamespace(**fields)) == expected
    # Mapping path; extra keys (e.g. reb) are ignored, missing keys → 0.
    assert game_score_from_row({**fields, "reb": 7}) == expected
    assert game_score_from_row({"pts": 10}) == 10.0
    assert game_score_from_row(SimpleNamespace(pts=10)) == 10.0


def test_fit_pythagorean_recovers_known_exponent() -> None:
    """With W/L = (PF/PA)^2, the fit returns x ≈ 2.0."""
    # ln(36/25)/ln(120/100) == 2.0 exactly.
    records = {i: {"w": 36, "l": 25, "pf": 120, "pa": 100} for i in range(25)}
    team_comp = {i: 1 for i in range(25)}
    x, n = fit_pythagorean(records, team_comp, {1})
    assert n == 25
    assert abs(x - 2.0) < 1e-6


def test_fit_pythagorean_falls_back_when_thin() -> None:
    """Too few decided records → the NBA-ish fallback exponent."""
    records = {0: {"w": 3, "l": 1, "pf": 110, "pa": 100}}
    x, n = fit_pythagorean(records, {0: 1}, {1})
    assert x == 13.0
    assert n == 1


def _ps_with_poss(pm: float, mp: float, poss: float, **box: float) -> PlayerSeason:
    ps = PlayerSeason(
        player_id=1,
        competition_id=1,
        primary_team_entry_id=1,
        year=2025,
        venue="las_vegas",
        box=_box(mp=mp, **box),
        team=Box(),
        opp=Box(),
        pm=pm,
    )
    ps.player_poss = poss
    ps.pct_min = mp / 40.0
    return ps


def test_bpm_split_reconstructs_bpm_and_centers_to_zero() -> None:
    """OBPM + DBPM == BPM, and the minute-weighted pool mean BPM is ~0."""
    coef = {f: 1.0 for f in BPM_FEATURES}
    pool = [
        _ps_with_poss(
            10,
            100,
            100,
            fgm=8,
            fg3m=2,
            ftm=4,
            fga=14,
            fta=5,
            oreb=2,
            dreb=6,
            ast=5,
            stl=2,
            blk=1,
            tov=2,
            pf=3,
        ),
        _ps_with_poss(
            -6,
            80,
            90,
            fgm=3,
            fg3m=1,
            ftm=2,
            fga=10,
            fta=3,
            oreb=1,
            dreb=3,
            ast=2,
            stl=1,
            blk=0,
            tov=4,
            pf=4,
        ),
        _ps_with_poss(
            2,
            120,
            110,
            fgm=5,
            fg3m=2,
            ftm=3,
            fga=12,
            fta=4,
            oreb=2,
            dreb=5,
            ast=4,
            stl=1,
            blk=1,
            tov=3,
            pf=2,
        ),
    ]
    by_pool = {1: pool}
    apply_sl_bpm([p for p in pool], by_pool, coef, intercept=-5.0)

    for ps in pool:
        bpm, obpm, dbpm = ps.metrics["bpm"], ps.metrics["obpm"], ps.metrics["dbpm"]
        assert bpm is not None and obpm is not None and dbpm is not None
        assert bpm == round(obpm + dbpm, 1)
        # Two value flavours: cumulative VORP on the standard MP/(48*82) yardstick
        # and an 82-game projection. The cumulative is never the larger magnitude
        # (a few games' worth of minutes is a fraction of a full season).
        vorp, vorp82 = ps.metrics["vorp"], ps.metrics["vorp82"]
        assert vorp is not None and vorp82 is not None
        assert abs(vorp) <= abs(vorp82)
    # At least one player has a non-trivial, clearly distinct pair of values.
    top = max(pool, key=lambda p: abs(p.metrics["vorp82"] or 0.0))
    top_vorp82, top_vorp = top.metrics["vorp82"], top.metrics["vorp"]
    assert top_vorp82 is not None and top_vorp is not None
    assert abs(top_vorp82) > abs(top_vorp) > 0.0
    wmp = sum(p.box.mp for p in pool)
    mean_bpm = sum((p.metrics["bpm"] or 0.0) * p.box.mp for p in pool) / wmp
    assert abs(mean_bpm) < 0.06  # within rounding of zero


def test_compute_metrics_keeps_box_rates_when_pool_ineligible() -> None:
    """Ineligible pools keep player/team-box rates but null calibrated composites."""
    ctx = LeagueContext(
        competition_id=1,
        year=2025,
        venue="las_vegas",
        lg=Box(),
        poss=0.0,
        team_games=0,
        adv_eligible=False,
    )
    ctx.pace = 100.0  # real pool (has possession data) but below the adv threshold
    ps = PlayerSeason(
        player_id=1,
        competition_id=1,
        primary_team_entry_id=1,
        year=2025,
        venue="las_vegas",
        box=_box(
            mp=100,
            pts=20,
            fgm=8,
            fga=15,
            fta=4,
            ftm=3,
            fg3m=2,
            fg3a=5,
            oreb=2,
            dreb=5,
            reb=7,
            ast=4,
            tov=3,
            gp=4,
        ),
        team=_box(
            mp=1000,
            pts=400,
            fgm=150,
            fga=320,
            fta=90,
            oreb=40,
            dreb=120,
            reb=160,
            tov=60,
        ),
        source_rates={"usg_pct": 34.5, "ast_pct": 15.0, "trb_pct": 12.5},
        opp=_box(
            mp=1000,
            pts=390,
            fgm=148,
            fga=318,
            fta=88,
            oreb=38,
            dreb=118,
            reb=156,
            tov=62,
        ),
    )
    compute_metrics(ps, ctx, ws_ppw_coeff=0.43)
    # Shooting/box still computed.
    assert ps.metrics["gmsc"] is not None
    assert ps.metrics["ts_pct"] is not None
    # pace / pts_per100 are raw possession measures — populated even when the pool
    # is ineligible, so per-100 works outside adv_eligible pools (issue #473).
    assert ps.metrics["pace"] is not None and ps.metrics["pace"] > 0
    assert ps.metrics["pts_per100"] is not None and ps.metrics["pts_per100"] > 0
    # Player/team-box rates do not require a complete league pool.
    assert ps.metrics["tov_pct"] == 15.2
    assert ps.metrics["usg_pct"] == 34.5
    assert ps.metrics["ast_pct"] == 15.0
    assert ps.metrics["trb_pct"] == 12.5
    # League-calibrated composites remain blanked.
    assert ps.metrics["per"] is None
    assert ps.metrics["ortg"] is None
    assert ps.metrics["ws"] is None
    assert ps.metrics["ws82"] is None


def test_line_rate_helpers_guard_empty_denominators() -> None:
    """The line-grain rate helpers return None instead of dividing by zero.

    A 0-FGA line has no free-throw rate, a line with no plays has no turnover
    rate, and AST% needs team context plus at least one teammate field goal.
    """
    assert ftr_line(fga=0, fta=2) is None
    assert ftr_line(fga=None, fta=None) is None
    assert tov_pct_line(fga=0, fta=0, tov=0) is None
    assert ast_pct_line(ast=3, fgm=5, mp=20, tm_mp=0, tm_fgm=30) is None
    # Played all 48 and made every team basket: teammate-FG denominator <= 0.
    assert ast_pct_line(ast=2, fgm=30, mp=48, tm_mp=240, tm_fgm=30) is None


def test_line_rate_helpers_match_bbref_formulas() -> None:
    """Known box lines produce the hand-computed BBRef rates."""
    # FTr = FTA / FGA.
    assert ftr_line(fga=15, fta=6) == 0.4
    # TOV% = 100 * TOV / (FGA + 0.44*FTA + TOV) = 100*3/(15+0.44*6+3) ≈ 14.5.
    assert tov_pct_line(fga=15, fta=6, tov=3) == round(
        100.0 * 3 / (15 + 0.44 * 6 + 3), 1
    )
    # AST% = 100 * AST / ((MP/(TmMP/5)) * TmFGM - FGM); 30 of 240 team minutes.
    expected = 100.0 * 5 / ((30.0 / 48.0) * 40 - 6)
    assert ast_pct_line(ast=5, fgm=6, mp=30, tm_mp=240, tm_fgm=40) == round(expected, 1)


def test_line_rate_helpers_match_season_compute() -> None:
    """Season-grain compute_metrics and the line helpers agree on one pool.

    Guards against the two formula homes drifting: feeding the same box totals
    through ``compute_metrics`` (an adv-eligible pool) and through the line
    helpers must produce identical FTr / TOV% / AST% values.
    """
    lg = _box(
        mp=2000,
        pts=800,
        fgm=300,
        fga=640,
        fg3m=60,
        fg3a=180,
        ftm=140,
        fta=180,
        oreb=80,
        dreb=240,
        reb=320,
        ast=180,
        stl=60,
        blk=30,
        tov=120,
        pf=160,
    )
    ctx = LeagueContext(
        competition_id=1,
        year=2025,
        venue="las_vegas",
        lg=lg,
        poss=1600.0,
        team_games=20,
        adv_eligible=True,
    )
    ctx.finalize()
    team = _box(
        mp=1000,
        pts=400,
        fgm=150,
        fga=320,
        fta=90,
        ftm=70,
        oreb=40,
        dreb=120,
        reb=160,
        ast=90,
        tov=60,
    )
    opp = _box(
        mp=1000,
        pts=390,
        fgm=148,
        fga=318,
        fta=88,
        ftm=66,
        oreb=38,
        dreb=118,
        reb=156,
        ast=88,
        tov=62,
    )
    ps = PlayerSeason(
        player_id=1,
        competition_id=1,
        primary_team_entry_id=1,
        year=2025,
        venue="las_vegas",
        box=_box(mp=100, pts=60, fgm=22, fga=45, fta=12, ftm=10, ast=15, tov=8, gp=4),
        team=team,
        opp=opp,
    )
    compute_metrics(ps, ctx, ws_ppw_coeff=0.43)
    b = ps.box
    assert ps.metrics["ftr"] == ftr_line(fga=b.fga, fta=b.fta)
    assert ps.metrics["tov_pct"] == tov_pct_line(fga=b.fga, fta=b.fta, tov=b.tov)
    assert ps.metrics["ast_pct"] == ast_pct_line(
        ast=b.ast, fgm=b.fgm, mp=b.mp, tm_mp=team.mp, tm_fgm=team.fgm
    )


def test_compute_metrics_skips_pace_when_pool_has_no_possession_data() -> None:
    """Pace/pts_per100 stay None for pools with a degenerate league pace.

    Pools reconstructed from season logs without team box data (mainly 2012-2016)
    have a near-zero league pace (ctx.pace ~0). Computing per-player pace off that
    yields explosive per-100, so possession rates must be left NULL for the whole
    pool — gated on the pool-level ctx.pace, not a per-row check.
    """
    ctx = LeagueContext(
        competition_id=1,
        year=2016,
        venue="las_vegas",
        lg=Box(),
        poss=0.0,
        team_games=0,
        adv_eligible=False,
    )
    ctx.pace = 2.0  # skeletal pool: league pace far below any real basketball value
    ps = PlayerSeason(
        player_id=1,
        competition_id=1,
        primary_team_entry_id=1,
        year=2016,
        venue="las_vegas",
        box=_box(mp=100, pts=28, fgm=12, fga=24, fta=4, ftm=2, fg3m=2, fg3a=6, gp=3),
        team=_box(mp=1000, pts=400, fgm=150, fga=320, fta=90, oreb=40, tov=60),
        opp=_box(mp=1000, pts=390, fgm=148, fga=318, fta=88, oreb=38, tov=62),
    )
    compute_metrics(ps, ctx, ws_ppw_coeff=0.43)
    assert ps.metrics["gmsc"] is not None  # box/shooting still fine
    assert ps.metrics["pace"] is None  # possession rates suppressed
    assert ps.metrics["pts_per100"] is None
