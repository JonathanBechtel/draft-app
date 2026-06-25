"""Unit tests for the draft-recap rise/fall helpers (no DB).

Covers the point-delta direction mapping and the movers split that power the
pick-by-pick board's arrows, colour gradient, and risers/fallers leaderboards.
"""

from __future__ import annotations

import pytest

from app.models.draft_results import RecapPick
from app.services.draft_results_service import (
    _SHADE_CAP,
    _delta_direction,
    split_movers,
)


@pytest.mark.parametrize(
    ("delta", "direction", "shade"),
    [
        (None, "unranked", 0.0),
        (0, "even", 0.0),
        (-9, "earlier", 9 / _SHADE_CAP),
        (4, "later", 4 / _SHADE_CAP),
        (-30, "earlier", 1.0),  # capped
        (50, "later", 1.0),  # capped
    ],
)
def test_delta_direction(delta, direction, shade) -> None:
    """Negative delta rises, positive falls; shade ramps to a capped 1.0."""
    got_dir, got_shade = _delta_direction(delta)
    assert got_dir == direction
    assert got_shade == pytest.approx(shade)


def _pick(pick: int, delta: int | None) -> RecapPick:
    return RecapPick(overall_pick=pick, round=1, round_pick=pick, delta=delta)


def test_split_movers_uses_point_delta_not_range() -> None:
    """Movers are ranked by point delta; even/unranked picks are excluded."""
    picks = [
        _pick(1, 0),  # on the number -> excluded
        _pick(13, -9),  # big riser (wide-band in real data)
        _pick(9, -3),  # small riser
        _pick(30, 12),  # big faller
        _pick(20, None),  # unranked -> excluded
    ]
    later, earlier = split_movers(picks, limit=10)
    assert [p.overall_pick for p in later] == [30]
    assert [p.overall_pick for p in earlier] == [13, 9]  # most negative first
