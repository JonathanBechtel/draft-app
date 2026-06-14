"""Unit tests for the Summer League schedule-rounds parser."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from app.services.summer_league.bracket import parse_schedule_rounds

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "summer_league"
    / "scheduleleaguev2_15_2024.json"
)


def test_parse_schedule_rounds_from_real_fixture() -> None:
    """The 2024 Vegas schedule yields the championship-bracket round labels."""
    payload = json.loads(_FIXTURE.read_text())
    rounds = parse_schedule_rounds(payload)

    # Known 2024 Vegas games (gameId -> round).
    assert rounds["1522400076"] == "Championship"
    assert rounds["1522400068"] == "Semifinals"
    assert rounds["1522400070"] == "Semifinals"
    # Pool-play games carry no sub-label and are excluded.
    assert "1522400001" not in rounds

    counts = Counter(rounds.values())
    assert counts["Championship"] == 1
    assert counts["Semifinals"] == 2
    assert counts["Consolation"] == 13


def test_parse_schedule_rounds_empty_and_missing() -> None:
    """Empty or label-less payloads return an empty mapping."""
    assert parse_schedule_rounds({}) == {}
    assert parse_schedule_rounds({"leagueSchedule": {"gameDates": []}}) == {}
    # A game with no gameSubLabel (pool play) is skipped.
    payload = {
        "leagueSchedule": {
            "gameDates": [{"games": [{"gameId": "x", "gameSubLabel": ""}]}]
        }
    }
    assert parse_schedule_rounds(payload) == {}
