"""Unit tests for the T8 competition-shaped capability declaration (#728).

``competition_capability_provides`` turns the availability flags normalization.py
owns and sets (``pbp_available``, ``shotchart_available``) into the canonical ``provides`` set
the shared capability model resolves ``requires`` against -- a thin, competition-shaped wrapper
over ``app.services.summer_league.capabilities.pool_provides``.
"""

from __future__ import annotations

from app.schemas.summer_league import SummerLeagueEdition, SummerLeagueDataQuality
from app.services.stats.capabilities import is_computable
from app.services.summer_league.capabilities import (
    BOX_PROVIDES,
    TEAM_OPPONENT_BOX_PROVIDES,
    competition_capability_provides,
)


def _competition(**kw: object) -> SummerLeagueEdition:
    base: dict[str, object] = dict(
        year=2026,
        league_id="10",
        venue_slug="las-vegas",
        display_name="Las Vegas Summer League",
        data_quality=SummerLeagueDataQuality.BOX_ONLY,
        pbp_available=False,
        shotchart_available=False,
    )
    base.update(kw)
    return SummerLeagueEdition(**base)  # type: ignore[arg-type]


def test_box_only_competition_cannot_compute_astd_pct() -> None:
    provides = competition_capability_provides(_competition())
    assert BOX_PROVIDES | TEAM_OPPONENT_BOX_PROVIDES <= provides
    assert not is_computable("astd_pct", provides)


def test_pbp_available_competition_can_compute_astd_pct() -> None:
    provides = competition_capability_provides(_competition(pbp_available=True))
    assert is_computable("astd_pct", provides)


def test_adv_eligible_flag_is_passed_through_since_it_is_not_owned_here() -> None:
    """adv_eligible lives on the metrics pool, not the competition row -- caller supplies it."""
    competition = _competition()
    assert not is_computable(
        "ws", competition_capability_provides(competition, adv_eligible=False)
    )
    assert is_computable(
        "ws", competition_capability_provides(competition, adv_eligible=True)
    )
