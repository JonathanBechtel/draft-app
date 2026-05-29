"""Unit tests for the shared player-name matcher and the combine importer guard.

These cover the regression that let a combine import create duplicate player
records for prospects that already existed under a suffix/punctuation variant
(e.g. "Darius Acuff Jr." vs "Darius Acuff"). The matcher must:

- match exact and suffix-/punctuation-variant names to the existing player,
- flag genuinely same-name collisions as ambiguous (so callers skip rather than
  mint a duplicate),
- report a clean miss for novel names (so callers may create).

The matcher logic is exercised through an in-memory lookup so no DB is needed.
"""

from typing import Optional, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.services.player_mention_service import (
    _lookup_from_rows,
    find_existing_player,
)
from scripts.ingest_combine import (
    _external_id_conflict,
    _reorder_leaked_suffix,
    get_or_create_player,
)


def _lookup(
    players: list[tuple[int, Optional[str]]],
    aliases: Optional[list[tuple[int, Optional[str], Optional[str]]]] = None,
):
    """Build an in-memory name lookup from (id, display_name) rows."""
    return _lookup_from_rows(players, aliases or [])


_NO_DB = cast(AsyncSession, None)  # find_existing_player never touches db when a lookup is passed


class TestFindExistingPlayer:
    @pytest.mark.asyncio
    async def test_exact_suffix_name_matches(self) -> None:
        """An exact 'Jr.' name resolves to the canonical record."""
        lookup = _lookup([(5384, "Darius Acuff Jr.")])
        match, ambiguous = await find_existing_player(
            _NO_DB, "Darius Acuff Jr.", lookup=lookup
        )
        assert ambiguous is False
        assert match is not None and match.player_id == 5384

    @pytest.mark.asyncio
    async def test_suffix_less_variant_matches_via_relaxed(self) -> None:
        """A suffix-less mention matches the canonical 'II'/'Jr.' record."""
        lookup = _lookup([(20, "Dereck Lively II")])
        match, ambiguous = await find_existing_player(
            _NO_DB, "Dereck Lively", lookup=lookup
        )
        assert ambiguous is False
        assert match is not None and match.player_id == 20

    @pytest.mark.asyncio
    async def test_punctuation_variant_matches(self) -> None:
        """Initials with periods match the punctuation-free canonical name."""
        lookup = _lookup([(5390, "AJ Dybantsa")])
        match, ambiguous = await find_existing_player(
            _NO_DB, "A.J. Dybantsa", lookup=lookup
        )
        assert ambiguous is False
        assert match is not None and match.player_id == 5390

    @pytest.mark.asyncio
    async def test_same_name_collision_is_ambiguous(self) -> None:
        """Two players with the same name resolve to ambiguous, not a guess."""
        lookup = _lookup([(10, "Chris Johnson"), (11, "Chris Johnson")])
        match, ambiguous = await find_existing_player(
            _NO_DB, "Chris Johnson", lookup=lookup
        )
        assert match is None
        assert ambiguous is True

    @pytest.mark.asyncio
    async def test_relaxed_collision_is_ambiguous(self) -> None:
        """A suffix-less query matching both a father and 'II' son is ambiguous."""
        lookup = _lookup([(30, "Gary Payton"), (31, "Gary Payton II")])
        match, ambiguous = await find_existing_player(
            _NO_DB, "Gary Payton Jr.", lookup=lookup
        )
        assert match is None
        assert ambiguous is True

    @pytest.mark.asyncio
    async def test_novel_name_is_clean_miss(self) -> None:
        """A genuinely new name reports not-found so callers may create it."""
        lookup = _lookup([(5384, "Darius Acuff Jr.")])
        match, ambiguous = await find_existing_player(
            _NO_DB, "Totally New Prospect", lookup=lookup
        )
        assert match is None
        assert ambiguous is False

    @pytest.mark.asyncio
    async def test_alias_name_matches(self) -> None:
        """A name that only matches via an alias still resolves."""
        lookup = _lookup(
            [(42, "Bruce Brown")],
            aliases=[(42, "Bruce Brown Jr.", "Bruce Brown")],
        )
        match, ambiguous = await find_existing_player(
            _NO_DB, "Bruce Brown Jr.", lookup=lookup
        )
        assert ambiguous is False
        assert match is not None and match.player_id == 42

class TestExternalIdConflict:
    """The id-conflict guard that keeps distinct identities from merging."""

    def test_no_external_id_never_conflicts(self) -> None:
        """A row without an external id can always link via name."""
        assert _external_id_conflict(["1629999"], None) is False

    def test_matched_player_without_ids_does_not_conflict(self) -> None:
        """Linking a prospect that has no id yet is allowed (the Acuff case)."""
        assert _external_id_conflict([], "1641722") is False

    def test_same_id_does_not_conflict(self) -> None:
        """The same id is the same identity — not a conflict."""
        assert _external_id_conflict(["1641722"], "1641722") is False

    def test_different_id_conflicts(self) -> None:
        """A different id of the same system marks a distinct identity (Payton)."""
        assert _external_id_conflict(["1629999"], "1641722") is True

    def test_conflict_if_any_existing_differs(self) -> None:
        """Any differing id triggers the guard even if another matches."""
        assert _external_id_conflict(["1641722", "1629999"], "1641722") is True


class TestReorderLeakedSuffix:
    def test_relocates_suffix_leaked_into_first(self) -> None:
        """A suffix token that landed in the first-name slot moves to suffix."""
        prefix, first, middle, last, suffix = _reorder_leaked_suffix(
            None, "Jr.", None, "Morez Johnson", None
        )
        assert suffix == "Jr."
        assert first is None
        assert last == "Morez Johnson"

    def test_relocates_suffix_leaked_into_prefix(self) -> None:
        """A suffix token in the prefix slot moves to suffix."""
        prefix, first, middle, last, suffix = _reorder_leaked_suffix(
            "II", "Robert", None, "Woodard", None
        )
        assert suffix == "II"
        assert prefix is None

    def test_leaves_well_formed_name_untouched(self) -> None:
        """A normal name with a real suffix is unchanged."""
        assert _reorder_leaked_suffix(None, "Morez", None, "Johnson", "Jr") == (
            None,
            "Morez",
            None,
            "Johnson",
            "Jr",
        )

    def test_does_not_treat_real_first_name_as_suffix(self) -> None:
        """A first name that is not a suffix token is left in place."""
        assert _reorder_leaked_suffix(None, "Vince", None, "Carter", None) == (
            None,
            "Vince",
            None,
            "Carter",
            None,
        )


class _FakeSession:
    """Minimal async-session stub for the get_or_create_player create path.

    Only ``add``/``flush`` are exercised when no match and no external id are
    supplied (find_existing_player resolves against the in-memory lookup).
    """

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)
        if isinstance(obj, PlayerMaster) and obj.id is None:
            obj.id = 9999  # stand in for the DB-assigned id

    async def flush(self) -> None:
        return None


class TestGetOrCreatePlayerNaming:
    @pytest.mark.asyncio
    async def test_leaked_suffix_without_raw_name_creates_clean_record(self) -> None:
        """A suffix leaked into `first` (no raw_player_name) creates a clean name.

        Regression for the bug where the display name was captured before the
        leaked suffix was relocated, so a new record was minted as
        "Jr Morez Johnson" (mangling the display name + slug).
        """
        session = _FakeSession()
        empty_lookup = _lookup_from_rows([], [])  # nothing matches -> create path
        created = await get_or_create_player(
            cast(AsyncSession, session),
            None,  # prefix
            "Jr",  # first  (the leaked suffix token)
            "Morez",  # middle
            "Johnson",  # last
            None,  # suffix
            raw_player_name=None,
            name_lookup=empty_lookup,
        )
        assert created is not None
        # Clean, suffix-last name — NOT the mangled "Jr Morez Johnson".
        assert created.display_name == "Morez Johnson Jr"
        assert created.suffix == "Jr."  # canonicalized by parse_player_name
        assert created.first_name == "Morez"
        assert created.last_name == "Johnson"
