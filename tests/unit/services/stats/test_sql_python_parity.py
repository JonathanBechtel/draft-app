"""Binding test (structural + arithmetic leg): SQL forms agree with Python forms.

Doc #2 §4's fallback (`docs/plans/summer-league-stat-engine-reuse-spec.md` §4,
ticket T6 / #727): rather than a formula-to-SQL compiler, each metric the
Explorer pushes into SQL gets its SQL form declared next to its Python form in
`app.services.stats.registry`, bound by a test asserting they agree. This file
is the no-database leg:

1. Because ``ts_pct_denom_expr`` / ``tov_pct_denom_expr`` only require ``box``
   to support ``+`` and ``*`` -- not that it return a SQLAlchemy expression --
   feeding them a callable that returns plain Python floats evaluates the
   *exact* declaration the Explorer's SQLAlchemy call sites use, entirely in
   Python. That is compared directly against
   :func:`app.services.stats.formulas.ts_pct_ratio` /
   :func:`app.services.stats.formulas.tov_pct_ratio`.
2. ``ts_pct_sql_text`` / ``tov_pct_sql_text`` are checked for byte-identical
   output against the literal SQL strings they replaced at
   ``app.services.summer_league_explorer_service`` (verified at HEAD before
   this change) -- proof the refactor changed *where* the formula lives, not
   *what SQL it emits*.
3. One structural check that a single declaration emits both the aggregate
   and row grain by varying only how ``box`` wraps a column name -- the
   "aggregate-vs-row-grain split" T6 calls out explicitly.

``tests/integration/services/stats/test_sql_python_parity.py`` is the
companion DB leg: the same declarations, executed against real Postgres rows.

**This test can fail.** Verified manually while implementing T6: changing
``ts_pct_denom_expr``'s ``0.44`` to ``0.55`` turned
``test_ts_pct_denom_expr_matches_python_ts_pct_ratio`` red (and the emitted
row-grain text no longer matched the byte-identical literal either); reverted
after confirming, and reported in the PR description.
"""

from __future__ import annotations

from typing import Callable

import pytest

from app.services.stats.formulas import game_score, ts_pct_ratio, tov_pct_ratio
from app.services.stats.inputs import StatInputs
from app.services.stats.registry import (
    game_score_sql_text,
    ts_pct_denom_expr,
    ts_pct_sql_text,
    tov_pct_denom_expr,
    tov_pct_sql_text,
)

# Box totals from the T1 fixture (tests/unit/test_stat_engine_parity.py /
# tests/integration/test_stat_engine_parity.py): pts=48 fga=40 fta=8 tov=8 --
# reusing the same numbers keeps this binding test's arithmetic traceable to
# the same hand-worked literals (TS%=55.1, TOV%=15.5) the harness already pins.
_BOX = {"pts": 48.0, "fga": 40.0, "fta": 8.0, "tov": 8.0}


def _float_box(values: dict[str, float]) -> Callable[[str], float]:
    """A ``box`` callable returning plain floats -- evaluates a SQL-form
    declaration as ordinary Python arithmetic, no SQLAlchemy/DB involved."""

    def box(name: str) -> float:
        return values[name]

    return box


def test_ts_pct_denom_expr_matches_python_ts_pct_ratio() -> None:
    """ts_pct_denom_expr, fed plain floats, agrees with ts_pct_ratio (unrounded).

    ``ts_pct_ratio`` is the *unrounded* ratio (see its docstring); the T1
    fixture's ``55.1`` literal is the season metric rounded to 1 decimal
    (``ts_pct_line`` / ``compute_metrics``), so the rounded form of this
    result is checked against that same literal as a sanity cross-check.
    """
    denom = ts_pct_denom_expr(_float_box(_BOX))
    got = 100.0 * _BOX["pts"] / denom
    want = ts_pct_ratio(pts=_BOX["pts"], fga=_BOX["fga"], fta=_BOX["fta"])
    assert want is not None
    assert got == pytest.approx(want)
    assert round(got, 1) == 55.1


def test_tov_pct_denom_expr_matches_python_tov_pct_ratio() -> None:
    """tov_pct_denom_expr, fed plain floats, agrees with tov_pct_ratio (unrounded).

    See :func:`test_ts_pct_denom_expr_matches_python_ts_pct_ratio` for why the
    rounded cross-check compares against the T1 fixture's ``15.5`` literal.
    """
    denom = tov_pct_denom_expr(_float_box(_BOX))
    got = 100.0 * _BOX["tov"] / denom
    want = tov_pct_ratio(fga=_BOX["fga"], fta=_BOX["fta"], tov=_BOX["tov"])
    assert want is not None
    assert got == pytest.approx(want)
    assert round(got, 1) == 15.5


def test_ts_pct_sql_text_row_grain_is_byte_identical_to_the_replaced_literal() -> None:
    """Row-grain text form matches the exact literal it replaced (verified at
    HEAD: app/services/summer_league_explorer_service.py:3283/3521 before T6)."""
    assert ts_pct_sql_text(lambda c: c) == "pts / NULLIF(2.0 * (fga + 0.44 * fta), 0)"


def test_ts_pct_sql_text_agg_grain_is_byte_identical_to_the_replaced_literal() -> None:
    """Aggregate-grain text form matches the exact literal it replaced
    (verified at HEAD: app/services/summer_league_explorer_service.py:2623/2691
    before T6)."""
    assert ts_pct_sql_text(lambda c: f"SUM({c})") == (
        "SUM(pts) / NULLIF(2.0 * (SUM(fga) + 0.44 * SUM(fta)), 0)"
    )


def test_tov_pct_sql_text_row_grain_is_byte_identical_to_the_replaced_literal() -> None:
    """Row-grain text form matches the exact literal it replaced (verified at
    HEAD: app/services/summer_league_explorer_service.py:3524 before T6)."""
    assert tov_pct_sql_text(lambda c: c) == (
        "tov * 100.0 / NULLIF(fga + 0.44 * fta + tov, 0)"
    )


def _recording_float_box(sink: list[str]) -> Callable[[str], float]:
    """A ``box`` callable that both records the field names it was asked for
    (in ``sink``) and returns a real float, so the arithmetic inside the SQL
    form still evaluates (unlike a callable that just returns the field name
    as a string, which would blow up on ``0.44 * box('fta')``)."""

    def box(name: str) -> float:
        sink.append(name)
        return 1.0

    return box


def _recording_string_box(sink: list[str]) -> Callable[[str], str]:
    """The string-returning twin of :func:`_recording_float_box`.

    ``game_score_sql_text`` builds SQL *text*, so its ``box`` callable must
    return ``str`` (unlike ``*_denom_expr``'s arithmetic-only ``box``, which
    only needs to support ``+``/``*`` and so tolerates plain floats).
    """

    def box(name: str) -> str:
        sink.append(name)
        return "1.0"

    return box


def test_ts_pct_denom_expr_emits_one_formula_across_both_grains() -> None:
    """One declaration, two grains: the only difference is how ``box`` wraps
    each field name, never a second copy of the ``2 * (FGA + 0.44*FTA)`` shape.
    """
    row_fields: list[str] = []
    agg_fields: list[str] = []
    ts_pct_denom_expr(_recording_float_box(row_fields))
    ts_pct_denom_expr(_recording_float_box(agg_fields))
    assert row_fields == agg_fields == ["fga", "fta"]


def test_tov_pct_denom_expr_field_order_matches_tov_pct_ratio() -> None:
    """tov_pct_denom_expr requests fga, fta, tov -- the same three inputs
    tov_pct_ratio's signature names, in the same denominator shape."""
    fields: list[str] = []
    tov_pct_denom_expr(_recording_float_box(fields))
    assert fields == ["fga", "fta", "tov"]


# --- game_score_sql_text (T9, #730) -----------------------------------------
#
# Folded into the registry by the stat-constant confinement ticket: the SQL
# form used to live as a private ``_game_score_sql`` helper in
# ``app.services.summer_league_explorer_service``, holding the Game Score
# weights (0.4/0.7/0.3) as a raw f-string outside app/services/stats/ -- T6
# consolidated ts_pct/tov_pct's SQL forms but explicitly left this one in
# place. These tests are the same byte-identical + arithmetic-parity shape
# T6 used for ts_pct_sql_text/tov_pct_sql_text above.

_GMSC_BOX = dict(
    pts=17.0,
    fgm=6.0,
    fga=15.0,
    ftm=3.0,
    fta=5.0,
    oreb=1.0,
    dreb=4.0,
    ast=3.0,
    stl=1.0,
    blk=0.0,
    tov=3.0,
    pf=2.0,
)


def test_game_score_sql_text_row_grain_is_byte_identical_to_the_replaced_literal() -> (
    None
):
    """Row-grain text form matches the exact literal ``_game_score_sql`` built
    in ``app.services.summer_league_explorer_service`` before T9."""
    assert game_score_sql_text(lambda c: c) == (
        "(pts + 0.4 * fgm - 0.7 * fga - 0.4 * (fta - ftm) + 0.7 * oreb "
        "+ 0.3 * dreb + stl + 0.7 * ast + 0.7 * blk - 0.4 * pf - tov)"
    )


def test_game_score_sql_text_matches_python_game_score() -> None:
    """Fed a box that renders each field as its string repr, the SQL text form
    evaluates (as arithmetic) to the same value as
    :func:`app.services.stats.formulas.game_score` on the same box line."""
    text = game_score_sql_text(lambda name: str(_GMSC_BOX[name]))
    got = eval(text)  # noqa: S307 -- arithmetic-only text built from a fixed box dict
    want = game_score(
        StatInputs(
            pts=_GMSC_BOX["pts"],
            fgm=_GMSC_BOX["fgm"],
            fga=_GMSC_BOX["fga"],
            ftm=_GMSC_BOX["ftm"],
            fta=_GMSC_BOX["fta"],
            oreb=_GMSC_BOX["oreb"],
            dreb=_GMSC_BOX["dreb"],
            ast=_GMSC_BOX["ast"],
            stl=_GMSC_BOX["stl"],
            blk=_GMSC_BOX["blk"],
            tov=_GMSC_BOX["tov"],
            pf=_GMSC_BOX["pf"],
        )
    )
    assert got == pytest.approx(want)


def test_game_score_sql_text_emits_one_formula_across_both_grains() -> None:
    """One declaration, two grains: the only difference is how ``box`` wraps
    each field name, never a second copy of the Game Score weights."""
    row_fields: list[str] = []
    agg_fields: list[str] = []
    game_score_sql_text(_recording_string_box(row_fields))
    game_score_sql_text(_recording_string_box(agg_fields))
    assert (
        row_fields
        == agg_fields
        == [
            "pts",
            "fgm",
            "fga",
            "fta",
            "ftm",
            "oreb",
            "dreb",
            "stl",
            "ast",
            "blk",
            "pf",
            "tov",
        ]
    )
