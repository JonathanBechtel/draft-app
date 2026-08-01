"""Unit tests for the Summer League capability adapter (T8, #728).

``app.services.summer_league.capabilities`` maps Summer League's own availability flags
(``pbp_available``, ``shotchart_available``, ``adv_eligible``) and fetched-row field presence
onto the canonical ``provides`` vocabulary the shared capability model
(``app.services.stats.capabilities``) resolves ``requires`` against. These tests pin the
mapping itself, independent of the Explorer call sites that consume it.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.stats.capabilities import is_computable
from app.services.summer_league.capabilities import (
    BOX_PROVIDES,
    PBP_PROVIDES,
    TEAM_OPPONENT_BOX_PROVIDES,
    pool_provides,
    row_provides,
    rows_provide,
)


def test_pool_provides_box_only_by_default() -> None:
    """No flags raised -> box inputs and usable team/opponent totals are provided."""
    provides = pool_provides(
        pbp_available=False, shotchart_available=False, adv_eligible=False
    )
    assert BOX_PROVIDES | TEAM_OPPONENT_BOX_PROVIDES <= provides
    assert not is_computable("astd_pct", provides)
    assert is_computable("ts_pct", provides)
    assert is_computable("usg_pct", provides)


def test_pool_provides_adds_pbp_tokens_when_available() -> None:
    provides = pool_provides(
        pbp_available=True, shotchart_available=False, adv_eligible=False
    )
    assert PBP_PROVIDES <= provides
    assert is_computable("astd_pct", provides)


def test_pool_provides_adds_adv_context_when_eligible() -> None:
    provides = pool_provides(
        pbp_available=False, shotchart_available=False, adv_eligible=True
    )
    assert {"team_box", "opponent_box", "pool_context"} <= provides
    assert is_computable("ws", provides)
    assert is_computable("bpm", provides)


def test_pool_provides_ineligible_pool_cannot_compute_pool_recalibrated_composites() -> None:
    provides = pool_provides(
        pbp_available=True, shotchart_available=True, adv_eligible=False
    )
    assert not is_computable("ws", provides)
    assert not is_computable("uper", provides)
    assert is_computable("orb_pct", provides)
    # astd_pct only needs PBP, not adv_eligible -- still computable.
    assert is_computable("astd_pct", provides)


def test_row_provides_pbp_when_ast_fgm_populated() -> None:
    row = SimpleNamespace(ast_fgm=6, unast_fgm=4)
    provides = row_provides(row)
    assert PBP_PROVIDES <= provides
    assert is_computable("astd_pct", provides)


def test_row_provides_no_pbp_when_fields_are_none() -> None:
    row = SimpleNamespace(ast_fgm=None, unast_fgm=None)
    provides = row_provides(row)
    assert provides == BOX_PROVIDES
    assert not is_computable("astd_pct", provides)


def test_row_provides_no_pbp_when_fields_are_absent() -> None:
    """A row that doesn't even carry the attributes behaves like a box-only source."""
    row = SimpleNamespace(pts=10)
    provides = row_provides(row)
    assert provides == BOX_PROVIDES


def test_rows_provide_unions_across_rows() -> None:
    """One PBP-era row among several box-only rows still makes the group PBP-capable."""
    rows = [
        SimpleNamespace(ast_fgm=None, unast_fgm=None),
        SimpleNamespace(ast_fgm=6, unast_fgm=4),
    ]
    provides = rows_provide(rows)
    assert PBP_PROVIDES <= provides
    assert is_computable("astd_pct", provides)


def test_rows_provide_no_pbp_when_no_row_has_it() -> None:
    rows = [
        SimpleNamespace(ast_fgm=None, unast_fgm=None),
        SimpleNamespace(ast_fgm=None, unast_fgm=None),
    ]
    provides = rows_provide(rows)
    assert provides == BOX_PROVIDES
    assert not is_computable("astd_pct", provides)


def test_rows_provide_empty_rows_is_box_only() -> None:
    assert rows_provide([]) == BOX_PROVIDES
