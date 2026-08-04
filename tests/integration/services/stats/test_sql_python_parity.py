"""Binding test (DB leg): SQL push-down forms agree with the Python forms (T6, #727).

Doc #2 §4's fallback -- one Python form, one SQL form per metric, declared
adjacently in `app.services.stats.registry` and bound by a test asserting
they agree -- rather than a formula-to-SQL compiler. The two metrics the
Explorer pushes into SQL (`ts_pct`, `tov_pct`) each get two SQL-form
declarations: a SQLAlchemy-expression form (`ts_pct_denom_expr`,
`tov_pct_denom_expr`) for the filter/HAVING call sites, and a raw-SQL-text
form (`ts_pct_sql_text`, `tov_pct_sql_text`) for the `ORDER BY` sort-expression
call sites in `app.services.summer_league_explorer_service`.

This test exercises both forms against a real database and asserts they
evaluate to the same number as `app.services.stats.formulas.ts_pct_ratio` /
`tov_pct_ratio` computed on the same box line -- at both row grain (one game
log) and aggregate grain (`SUM(...)` over several game logs), since one
registry declaration must emit both shapes (the "aggregate-vs-row-grain
split" T6 calls out explicitly).

`tests/unit/services/stats/test_sql_python_parity.py` covers the same
declarations without a database (`box` fed plain floats, plus a literal-text
check against the exact strings these declarations replaced). This file is
the "run it against a real DB" half of the DoD, deliberately independent of
`tests/integration/test_stat_engine_parity.py` (T1's four-surface harness),
which this ticket must not edit.

Requires TEST_DATABASE_URL and PYTEST_ALLOW_DB=1.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
)
from app.services.stats.formulas import efg_pct_ratio, ts_pct_ratio, tov_pct_ratio
from app.services.stats.registry import (
    efg_pct_num_expr,
    ts_pct_denom_expr,
    ts_pct_sql_text,
    tov_pct_denom_expr,
    tov_pct_sql_text,
)
from tests.integration.conftest import make_player

# Two distinct per-game box lines (deliberately not identical, and none of
# fga/fta/tov equal to 0 or 1) so a formula-shape bug -- e.g. a dropped 0.44
# term, or a swapped SUM()/bare-column box -- cannot hide behind a degenerate
# input on either the row-grain or the summed aggregate-grain check.
_LINE_A = dict(
    minutes_seconds=1800,
    pts=17,
    fgm=6,
    fga=15,
    fg3m=2,
    fg3a=5,
    ftm=3,
    fta=5,
    oreb=1,
    dreb=4,
    reb=5,
    ast=3,
    stl=1,
    blk=0,
    tov=3,
    pf=2,
)
_LINE_B = dict(
    minutes_seconds=1500,
    pts=9,
    fgm=3,
    fga=11,
    fg3m=1,
    fg3a=4,
    ftm=2,
    fta=3,
    oreb=2,
    dreb=2,
    reb=4,
    ast=1,
    stl=0,
    blk=1,
    tov=2,
    pf=3,
)


async def _seed_two_game_logs(db: AsyncSession) -> tuple[int, int]:
    """Seed a player with two game logs; return their (id_a, id_b)."""
    comp = SummerLeagueEdition(
        year=2025,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2025 las_vegas sql-parity",
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None

    team = SummerLeagueTeamEntry(
        competition_id=comp.id,
        nba_stats_team_id="t-sql-parity",
        raw_team_name="SQL Parity Team",
        raw_team_abbreviation="SQP",
        team_slug="sql-parity-team",
    )
    db.add(team)
    await db.flush()
    assert team.id is not None

    player = make_player("Sql", "Parity")
    db.add(player)
    await db.flush()
    assert player.id is not None

    sp = SummerLeagueSourceRecord(
        nba_stats_person_id="sp-sql-parity",
        raw_player_name=player.display_name or "P",
        normalized_name=(player.display_name or "p").lower(),
        canonical_player_id=player.id,
    )
    db.add(sp)
    await db.flush()
    assert sp.id is not None

    ids: list[int] = []
    for i, line in enumerate((_LINE_A, _LINE_B)):
        game = SummerLeagueGame(
            competition_id=comp.id,
            nba_stats_game_id=f"g-sql-parity-{i}",
            game_date=date(2025, 7, 6 + i),
            home_team_entry_id=team.id,
            away_team_entry_id=team.id,
            home_score=80,
            away_score=72,
        )
        db.add(game)
        await db.flush()
        assert game.id is not None
        log = SummerLeaguePlayerGameLog(
            competition_id=comp.id,
            game_id=game.id,
            team_entry_id=team.id,
            source_player_id=sp.id,
            player_id=player.id,
            nba_stats_person_id=sp.nba_stats_person_id,
            raw_player_name=player.display_name or "P",
            **line,
        )
        db.add(log)
        await db.flush()
        assert log.id is not None
        ids.append(log.id)
    return ids[0], ids[1]


@pytest.mark.asyncio
async def test_row_grain_sql_text_forms_match_python_ratios(
    db_session: AsyncSession,
) -> None:
    """Row-grain SQL text forms (bare column labels) match ts_pct_ratio/tov_pct_ratio.

    ``ts_pct_sql_text``'s row-grain output is an unscaled ratio (the ORDER BY
    call sites only need monotonicity, not the *100 percent scale) so it is
    compared to ``ts_pct_ratio`` after multiplying by 100; ``tov_pct_sql_text``
    already carries the *100.0 scale and compares directly.
    """
    log_id, _ = await _seed_two_game_logs(db_session)
    await db_session.commit()

    stmt = text(
        f"SELECT ({ts_pct_sql_text(lambda c: c)}) AS ts_ratio, "
        f"({tov_pct_sql_text(lambda c: c)}) AS tov_pct "
        "FROM summer_league_player_game_logs WHERE id = :id"
    )
    row = (await db_session.execute(stmt, {"id": log_id})).one()

    want_ts = ts_pct_ratio(pts=_LINE_A["pts"], fga=_LINE_A["fga"], fta=_LINE_A["fta"])
    want_tov = tov_pct_ratio(
        fga=_LINE_A["fga"], fta=_LINE_A["fta"], tov=_LINE_A["tov"]
    )
    assert want_ts is not None and want_tov is not None
    assert float(row.ts_ratio) * 100.0 == pytest.approx(want_ts)
    assert float(row.tov_pct) == pytest.approx(want_tov)


@pytest.mark.asyncio
async def test_aggregate_grain_sql_text_forms_match_python_ratios_on_summed_box(
    db_session: AsyncSession,
) -> None:
    """Aggregate-grain SQL text form (SUM(...)-wrapped) matches the Python ratio
    computed on the two rows' *summed* box totals -- the recombinable rollup
    class's rule (recompute from summed components, never average per-row
    ratios).
    """
    _, _log_id_b = await _seed_two_game_logs(db_session)
    await db_session.commit()

    stmt = text(
        f"SELECT ({ts_pct_sql_text(lambda c: f'SUM({c})')}) AS ts_ratio "
        "FROM summer_league_player_game_logs "
        "WHERE nba_stats_person_id = 'sp-sql-parity'"
    )
    row = (await db_session.execute(stmt)).one()

    summed_pts = _LINE_A["pts"] + _LINE_B["pts"]
    summed_fga = _LINE_A["fga"] + _LINE_B["fga"]
    summed_fta = _LINE_A["fta"] + _LINE_B["fta"]
    want_ts = ts_pct_ratio(pts=summed_pts, fga=summed_fga, fta=summed_fta)
    assert want_ts is not None
    assert float(row.ts_ratio) * 100.0 == pytest.approx(want_ts)


@pytest.mark.asyncio
async def test_row_grain_sqlalchemy_expr_forms_match_python_ratios(
    db_session: AsyncSession,
) -> None:
    """Row-grain SQLAlchemy-expression forms (bare ORM columns) match the ratios.

    Exercises the filter/HAVING call sites' shape directly:
    ``100.0 * numerator / func.nullif(<denom_expr>(box), 0)``.
    """
    log_id, _ = await _seed_two_game_logs(db_session)
    await db_session.commit()

    pgl = SummerLeaguePlayerGameLog
    ts_denom = ts_pct_denom_expr(lambda name: getattr(pgl, name))
    tov_denom = tov_pct_denom_expr(lambda name: getattr(pgl, name))
    stmt = select(
        (100.0 * pgl.pts / func.nullif(ts_denom, 0)).label("ts_pct"),  # type: ignore[operator]
        (100.0 * pgl.tov / func.nullif(tov_denom, 0)).label("tov_pct"),  # type: ignore[operator]
    ).where(pgl.id == log_id)  # type: ignore[arg-type]
    row = (await db_session.execute(stmt)).one()

    want_ts = ts_pct_ratio(pts=_LINE_A["pts"], fga=_LINE_A["fga"], fta=_LINE_A["fta"])
    want_tov = tov_pct_ratio(
        fga=_LINE_A["fga"], fta=_LINE_A["fta"], tov=_LINE_A["tov"]
    )
    assert want_ts is not None and want_tov is not None
    assert float(row.ts_pct) == pytest.approx(want_ts)
    assert float(row.tov_pct) == pytest.approx(want_tov)


@pytest.mark.asyncio
async def test_aggregate_grain_sqlalchemy_expr_form_matches_python_ratio_on_summed_box(
    db_session: AsyncSession,
) -> None:
    """Aggregate-grain SQLAlchemy-expression form (``func.sum``-wrapped) matches
    the Python ratio on the two rows' summed box totals -- the same one
    ``ts_pct_denom_expr`` declaration used by the row-grain test above, only
    the ``box`` callable changes (``func.sum(getattr(pgl, name))``).
    """
    await _seed_two_game_logs(db_session)
    await db_session.commit()

    pgl = SummerLeaguePlayerGameLog
    ts_denom = ts_pct_denom_expr(lambda name: func.sum(getattr(pgl, name)))
    stmt = select(
        (100.0 * func.sum(pgl.pts) / func.nullif(ts_denom, 0)).label(  # type: ignore[arg-type]
            "ts_pct"
        )
    ).where(pgl.nba_stats_person_id == "sp-sql-parity")  # type: ignore[arg-type]
    row = (await db_session.execute(stmt)).one()

    summed_pts = _LINE_A["pts"] + _LINE_B["pts"]
    summed_fga = _LINE_A["fga"] + _LINE_B["fga"]
    summed_fta = _LINE_A["fta"] + _LINE_B["fta"]
    want_ts = ts_pct_ratio(pts=summed_pts, fga=summed_fga, fta=summed_fta)
    assert want_ts is not None
    assert float(row.ts_pct) == pytest.approx(want_ts)


@pytest.mark.asyncio
async def test_row_grain_efg_num_expr_matches_python_efg_pct_ratio(
    db_session: AsyncSession,
) -> None:
    """Row-grain ``efg_pct_num_expr`` (bare ORM columns) matches efg_pct_ratio.

    ``efg_pct_num_expr`` was added by the Phase 2 QA gate (#731) after its
    demonstration that the Explorer's hand-written eFG% filter expressions
    could drift silently from the displayed value. This exercises the exact
    filter call-site shape (``100.0 * <num_expr> / func.nullif(fga, 0)``,
    see ``_per_comp_metric_where``) against a real row, so an edit to the
    expression form's half-credit weight fails here even though both guards
    are structurally blind to it.
    """
    log_id, _ = await _seed_two_game_logs(db_session)
    await db_session.commit()

    pgl = SummerLeaguePlayerGameLog
    efg_num = efg_pct_num_expr(lambda name: getattr(pgl, name))
    stmt = select(
        (100.0 * efg_num / func.nullif(pgl.fga, 0)).label("efg_pct")  # type: ignore[arg-type]
    ).where(pgl.id == log_id)  # type: ignore[arg-type]
    row = (await db_session.execute(stmt)).one()

    want_efg = efg_pct_ratio(
        fgm=_LINE_A["fgm"], fga=_LINE_A["fga"], fg3m=_LINE_A["fg3m"]
    )
    assert want_efg is not None
    assert float(row.efg_pct) == pytest.approx(want_efg)


@pytest.mark.asyncio
async def test_aggregate_grain_efg_num_expr_matches_python_efg_pct_ratio_on_summed_box(
    db_session: AsyncSession,
) -> None:
    """Aggregate-grain ``efg_pct_num_expr`` (``func.sum``-wrapped) matches the
    Python ratio on the two rows' summed box totals -- the same declaration as
    the row-grain test above, only the ``box`` callable changes (the career
    filter's shape at ``_career_metric_having``)."""
    await _seed_two_game_logs(db_session)
    await db_session.commit()

    pgl = SummerLeaguePlayerGameLog
    efg_num = efg_pct_num_expr(lambda name: func.sum(getattr(pgl, name)))
    stmt = select(
        (100.0 * efg_num / func.nullif(func.sum(pgl.fga), 0)).label(  # type: ignore[arg-type]
            "efg_pct"
        )
    ).where(pgl.nba_stats_person_id == "sp-sql-parity")  # type: ignore[arg-type]
    row = (await db_session.execute(stmt)).one()

    want_efg = efg_pct_ratio(
        fgm=_LINE_A["fgm"] + _LINE_B["fgm"],
        fga=_LINE_A["fga"] + _LINE_B["fga"],
        fg3m=_LINE_A["fg3m"] + _LINE_B["fg3m"],
    )
    assert want_efg is not None
    assert float(row.efg_pct) == pytest.approx(want_efg)
