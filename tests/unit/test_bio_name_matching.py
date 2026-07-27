"""Unit tests for bio-ingest name matching (`_deterministic_match`).

These guard against the namesake-contamination bug where a different player
who merely shares a surname and first initial (e.g. "Derek" vs "Dylan"
Harper) was matched onto an existing record, merging one player's bio and
stats onto another. The matcher must only accept exact first+last name
matches and otherwise return ``None`` (unmatched).
"""

from collections import defaultdict
from typing import Dict, List

from app.services.player_bio.matching import _deterministic_match, _norm
from app.schemas.players_master import PlayerMaster


def _index(*players: PlayerMaster):
    """Build the (last_name_idx, pm_by_id) lookups the matcher expects."""
    last_name_idx: Dict[str, List[int]] = defaultdict(list)
    pm_by_id: Dict[int, PlayerMaster] = {}
    for p in players:
        assert p.id is not None
        pm_by_id[p.id] = p
        if p.last_name:
            last_name_idx[_norm(p.last_name)].append(p.id)
    return last_name_idx, pm_by_id


def test_exact_first_and_last_name_matches():
    """An exact first+last name match returns the player's id."""
    dylan = PlayerMaster(id=1, first_name="Dylan", last_name="Harper")
    idx, by_id = _index(dylan)
    assert _deterministic_match("Dylan Harper", idx, by_id) == 1


def test_same_surname_same_initial_different_first_name_is_unmatched():
    """Derek Harper must NOT match the existing Dylan Harper record."""
    dylan = PlayerMaster(id=1, first_name="Dylan", last_name="Harper")
    idx, by_id = _index(dylan)
    # Shares surname "Harper" and initial "D" but is a different person.
    assert _deterministic_match("Derek Harper", idx, by_id) is None


def test_unique_initial_no_longer_forces_a_match():
    """Even when only one same-initial candidate exists, no guess is made."""
    cody = PlayerMaster(id=7, first_name="Cody", last_name="Williams")
    idx, by_id = _index(cody)
    # "Cole Williams" shares surname + "C" initial; previously this matched.
    assert _deterministic_match("Cole Williams", idx, by_id) is None


def test_correct_player_still_matches_among_namesakes():
    """The exact-name player is selected even when namesakes share surname."""
    dylan = PlayerMaster(id=1, first_name="Dylan", last_name="Harper")
    derek = PlayerMaster(id=2, first_name="Derek", last_name="Harper")
    idx, by_id = _index(dylan, derek)
    assert _deterministic_match("Derek Harper", idx, by_id) == 2
    assert _deterministic_match("Dylan Harper", idx, by_id) == 1


def test_unknown_surname_is_unmatched():
    """A surname with no candidates returns None."""
    dylan = PlayerMaster(id=1, first_name="Dylan", last_name="Harper")
    idx, by_id = _index(dylan)
    assert _deterministic_match("Victor Wembanyama", idx, by_id) is None
