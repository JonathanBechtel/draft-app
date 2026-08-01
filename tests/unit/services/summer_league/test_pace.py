"""Unit tests for competition-grain Summer League possession adapters."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.summer_league.pace import player_possessions_from_rows
from app.services.stats.inputs import BOX_INT_FIELDS, StatInputs


def _row(
    *,
    game_id: int,
    team_fga: int,
    team_minutes: float = 180.0,
    opponent_minutes: float = 180.0,
) -> SimpleNamespace:
    """Build one labelled query row with complete team/opponent boxes."""
    values: dict[str, object] = {
        "game_id": game_id,
        "team_entry_id": 1,
        "minutes_seconds": 1800,
        "team_mp": team_minutes,
        "opp_mp": opponent_minutes,
    }
    for prefix in ("team", "opp"):
        for field_name in BOX_INT_FIELDS:
            values[f"{prefix}_{field_name}"] = 0
    values["team_fga"] = team_fga
    values["team_fgm"] = team_fga
    values["team_tov"] = 10
    values["opp_fga"] = team_fga
    values["opp_fgm"] = team_fga
    values["opp_tov"] = 10
    return SimpleNamespace(**values)


def test_possessions_pool_team_boxes_before_applying_player_minutes() -> None:
    """Variable game pace uses the competition-pool engine denominator."""
    rows = [_row(game_id=1, team_fga=50), _row(game_id=2, team_fga=100)]

    team = StatInputs(mp=360, fga=150, fgm=150, tov=20)
    opponent = StatInputs(mp=360, fga=150, fgm=150, tov=20)
    expected = team.poss(opponent) * 60.0 / (team.mp / 5.0)

    assert player_possessions_from_rows(rows) == pytest.approx(expected)


def test_partial_team_box_is_unavailable() -> None:
    """A team box below the engine completeness threshold yields no estimate."""
    rows = [_row(game_id=1, team_fga=50, team_minutes=120)]

    assert player_possessions_from_rows(rows) is None
