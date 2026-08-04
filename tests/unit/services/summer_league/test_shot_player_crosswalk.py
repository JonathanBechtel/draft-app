"""Unit tests for the legacy shot-id -> canonical person-id crosswalk (issue #467).

Pre-2017 ``shotchartdetail`` returns a legacy 5-digit ``PLAYER_ID`` namespace for
undrafted players that does not match the canonical box/season-log person-ids.
``build_shot_player_crosswalk`` recovers the link by fingerprinting each player's
``(FGA, FGM, 3PA, 3PM)`` line per team and mapping only bijectively-unique matches.
No database required.
"""

from __future__ import annotations

from itertools import count

from app.services.sources.summer_league.normalization import (
    ParsedPlayerBoxRow,
    ParsedShotEvent,
    build_shot_player_crosswalk,
    _remap_shot_row,
)

_EVENT_IDS = count(1)


def _shot(
    person_id: str,
    team_id: str,
    *,
    made: bool,
    three: bool,
    name: str = "",
) -> ParsedShotEvent:
    """Build a single shot attempt for the given player."""
    return ParsedShotEvent(
        nba_stats_game_id="G1",
        nba_stats_game_event_id=next(_EVENT_IDS),
        nba_stats_person_id=person_id,
        raw_player_name=name or person_id,
        nba_stats_team_id=team_id,
        period=1,
        minutes_remaining=5,
        seconds_remaining=0,
        loc_x=0,
        loc_y=0,
        shot_distance=1,
        shot_type="3PT Field Goal" if three else "2PT Field Goal",
        shot_zone_basic=None,
        shot_zone_area=None,
        shot_zone_range=None,
        action_type="Jump Shot",
        made=made,
    )


def _shots(
    person_id: str,
    team_id: str,
    *,
    fga: int,
    fgm: int,
    fg3a: int,
    fg3m: int,
    name: str = "",
) -> list[ParsedShotEvent]:
    """Expand a (FGA, FGM, 3PA, 3PM) line into individual shot attempts."""
    rows: list[ParsedShotEvent] = []
    for _ in range(fg3m):
        rows.append(_shot(person_id, team_id, made=True, three=True, name=name))
    for _ in range(fg3a - fg3m):
        rows.append(_shot(person_id, team_id, made=False, three=True, name=name))
    two_m = fgm - fg3m
    two_a = (fga - fg3a) - two_m
    for _ in range(two_m):
        rows.append(_shot(person_id, team_id, made=True, three=False, name=name))
    for _ in range(two_a):
        rows.append(_shot(person_id, team_id, made=False, three=False, name=name))
    return rows


def _box(
    person_id: str,
    team_id: str,
    *,
    fga: int,
    fgm: int,
    fg3a: int,
    fg3m: int,
    name: str = "",
) -> ParsedPlayerBoxRow:
    return ParsedPlayerBoxRow(
        game_id="G1",
        nba_stats_person_id=person_id,
        raw_player_name=name or person_id,
        nba_stats_team_id=team_id,
        fga=fga,
        fgm=fgm,
        fg3a=fg3a,
        fg3m=fg3m,
    )


def test_unique_signature_maps_legacy_to_canonical() -> None:
    """A legacy shot id with a unique per-team line maps to the box person id."""
    shots = _shots("51845", "T1", fga=13, fgm=6, fg3a=8, fg3m=5)
    box = [_box("203503", "T1", fga=13, fgm=6, fg3a=8, fg3m=5, name="Tony Snell")]
    assert build_shot_player_crosswalk(shots, box) == {"51845": "203503"}


def test_already_canonical_shot_id_is_not_remapped() -> None:
    """A shot id already present in the box is left out of the crosswalk."""
    shots = _shots("203503", "T1", fga=13, fgm=6, fg3a=8, fg3m=5)
    box = [_box("203503", "T1", fga=13, fgm=6, fg3a=8, fg3m=5)]
    assert build_shot_player_crosswalk(shots, box) == {}


def test_ambiguous_box_side_is_skipped() -> None:
    """Two box players sharing the signature block the match (no guess)."""
    shots = _shots("51001", "T1", fga=5, fgm=2, fg3a=1, fg3m=1)
    box = [
        _box("2001", "T1", fga=5, fgm=2, fg3a=1, fg3m=1),
        _box("2002", "T1", fga=5, fgm=2, fg3a=1, fg3m=1),
    ]
    assert build_shot_player_crosswalk(shots, box) == {}


def test_ambiguous_shot_side_is_skipped() -> None:
    """Two legacy shot players sharing the signature both stay unresolved."""
    shots = _shots("51001", "T1", fga=5, fgm=2, fg3a=1, fg3m=1) + _shots(
        "51002", "T1", fga=5, fgm=2, fg3a=1, fg3m=1
    )
    box = [_box("2001", "T1", fga=5, fgm=2, fg3a=1, fg3m=1)]
    assert build_shot_player_crosswalk(shots, box) == {}


def test_no_box_match_is_skipped() -> None:
    """A legacy shot line with no matching box line is not mapped."""
    shots = _shots("51001", "T1", fga=5, fgm=2, fg3a=1, fg3m=1)
    box = [_box("2001", "T1", fga=9, fgm=4, fg3a=0, fg3m=0)]
    assert build_shot_player_crosswalk(shots, box) == {}


def test_same_signature_different_team_does_not_match() -> None:
    """Matching is scoped per team; identical lines on other teams don't cross."""
    shots = _shots("51001", "T1", fga=5, fgm=2, fg3a=1, fg3m=1)
    box = [_box("2001", "T2", fga=5, fgm=2, fg3a=1, fg3m=1)]
    assert build_shot_player_crosswalk(shots, box) == {}


def test_two_distinct_lines_both_map() -> None:
    """Distinct signatures on one team each resolve independently."""
    shots = _shots("51001", "T1", fga=5, fgm=2, fg3a=1, fg3m=1) + _shots(
        "51002", "T1", fga=10, fgm=7, fg3a=5, fg3m=3
    )
    box = [
        _box("2001", "T1", fga=5, fgm=2, fg3a=1, fg3m=1),
        _box("2002", "T1", fga=10, fgm=7, fg3a=5, fg3m=3),
    ]
    assert build_shot_player_crosswalk(shots, box) == {
        "51001": "2001",
        "51002": "2002",
    }


def test_remap_rewrites_person_id_and_adopts_box_name() -> None:
    """A mapped shot takes the canonical id and the box player's name."""
    shot = _shot("51845", "T1", made=True, three=True)
    remapped = _remap_shot_row(shot, {"51845": "203503"}, {"203503": "Tony Snell"})
    assert remapped.nba_stats_person_id == "203503"
    assert remapped.raw_player_name == "Tony Snell"
    # Non-identity fields are preserved.
    assert remapped.nba_stats_game_event_id == shot.nba_stats_game_event_id
    assert remapped.made is True


def test_remap_leaves_unmapped_shot_unchanged() -> None:
    """A shot id absent from the crosswalk is returned untouched."""
    shot = _shot("99999", "T1", made=False, three=False, name="Someone")
    remapped = _remap_shot_row(shot, {"51845": "203503"}, {"203503": "Tony Snell"})
    assert remapped is shot
