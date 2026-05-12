"""Player mention resolution service.

Resolves player name strings to database IDs by checking normalized
display names, aliases, and optionally creating stub records for
unknown players.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_lifecycle import CareerStatus, DraftStatus, PlayerLifecycle
from app.schemas.players_master import PlayerMaster

logger = logging.getLogger(__name__)

_SUFFIX_CANONICAL_MAP = {
    "jr": "Jr.",
    "junior": "Jr.",
    "sr": "Sr.",
    "senior": "Sr.",
    "ii": "II",
    "iii": "III",
    "iv": "IV",
    "v": "V",
    "vi": "VI",
}

_PUNCT_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u00b4": "'",
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
    }
)


@dataclass(frozen=True, slots=True)
class PlayerMatch:
    """Result of resolving a player name to a database record."""

    player_id: int
    display_name: str
    matched_via: str  # "display_name", "alias", or "stub_created"


@dataclass(frozen=True, slots=True)
class ParsedPlayerName:
    """Structured player name fields derived from a freeform full name."""

    first_name: str
    middle_name: Optional[str]
    last_name: Optional[str]
    suffix: Optional[str]


@dataclass(slots=True)
class _LookupEntry:
    """A candidate player row stored in the in-memory name lookup."""

    player_id: int
    display_name: str
    matched_via: str


@dataclass(slots=True)
class _PlayerNameLookup:
    """Normalized name lookup tables for resolving mention candidates."""

    display_exact: dict[str, dict[int, _LookupEntry]]
    alias_exact: dict[str, dict[int, _LookupEntry]]
    display_relaxed: dict[str, dict[int, _LookupEntry]]
    alias_relaxed: dict[str, dict[int, _LookupEntry]]


def _collapse_whitespace(value: str) -> str:
    """Collapse repeated whitespace and trim the result."""
    return re.sub(r"\s+", " ", value.strip())


def _ascii_fold(value: str) -> str:
    """Fold Unicode text to a simple ASCII-ish representation."""
    normalized = unicodedata.normalize("NFKD", value.translate(_PUNCT_TRANSLATION))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _canonical_suffix(token: str) -> Optional[str]:
    """Return the canonical suffix spelling for a raw token if recognized."""
    normalized = re.sub(r"[^a-z0-9]", "", _ascii_fold(token).lower())
    return _SUFFIX_CANONICAL_MAP.get(normalized)


def _normalized_token(token: str) -> str:
    """Normalize a token for exact string comparison."""
    return re.sub(r"[^a-z0-9]", "", _ascii_fold(token).lower())


def _can_create_stub_player(full_name: str) -> bool:
    """Return whether a mention is specific enough to become a stub player.

    Single-token mentions like "Wagler" are too ambiguous and tend to create
    low-quality duplicate placeholder players from article prose.
    """
    collapsed = _collapse_whitespace(full_name)
    if not collapsed:
        return False

    raw_tokens = collapsed.split()
    if raw_tokens and _canonical_suffix(raw_tokens[-1]) is not None:
        raw_tokens = raw_tokens[:-1]

    normalized_tokens = [_normalized_token(token) for token in raw_tokens]
    normalized_tokens = [token for token in normalized_tokens if token]
    return len(normalized_tokens) >= 2


def parse_player_name(full_name: str) -> ParsedPlayerName:
    """Parse a full name into first/middle/last/suffix fields."""
    parts = _collapse_whitespace(full_name).split()
    if len(parts) == 0:
        return ParsedPlayerName("", None, None, None)

    suffix: Optional[str] = None
    maybe_suffix = _canonical_suffix(parts[-1])
    if maybe_suffix is not None:
        suffix = maybe_suffix
        parts = parts[:-1]

    if len(parts) == 0:
        return ParsedPlayerName("", None, None, suffix)
    if len(parts) == 1:
        return ParsedPlayerName(parts[0], None, None, suffix)

    first_name = parts[0]
    last_name = parts[-1]
    middle_name = " ".join(parts[1:-1]) if len(parts) > 2 else None
    return ParsedPlayerName(first_name, middle_name, last_name, suffix)


def split_name(full_name: str) -> tuple[str, Optional[str]]:
    """Split a full name into (first_name, last_name), excluding suffixes.

    Args:
        full_name: Full player name string (e.g. "Cooper Flagg")

    Returns:
        Tuple of (first_name, last_name). last_name is None for single-word names.
    """
    parsed = parse_player_name(full_name)
    if parsed.middle_name and parsed.last_name:
        return (parsed.first_name, f"{parsed.middle_name} {parsed.last_name}")
    return (parsed.first_name, parsed.last_name)


def _normalized_name_key(
    full_name: str,
    *,
    ignore_suffix: bool = False,
    ignore_middle_initials: bool = False,
) -> str:
    """Build a comparable normalized key from a player name string."""
    collapsed = _collapse_whitespace(full_name)
    if not collapsed:
        return ""

    raw_tokens = collapsed.split()
    suffix = _canonical_suffix(raw_tokens[-1]) if raw_tokens else None
    if suffix is not None:
        raw_tokens = raw_tokens[:-1]

    normalized_tokens = [_normalized_token(token) for token in raw_tokens]
    normalized_tokens = [token for token in normalized_tokens if token]
    if not normalized_tokens:
        return ""

    if ignore_middle_initials and len(normalized_tokens) > 2:
        normalized_tokens = [
            normalized_tokens[0],
            *[token for token in normalized_tokens[1:-1] if len(token) > 1],
            normalized_tokens[-1],
        ]

    if not ignore_suffix and suffix is not None:
        normalized_tokens.append(_normalized_token(_canonical_suffix(suffix) or suffix))

    return " ".join(token.lower() for token in normalized_tokens)


def _add_lookup_entry(
    lookup: dict[str, dict[int, _LookupEntry]],
    key: str,
    entry: _LookupEntry,
) -> None:
    """Insert a normalized name lookup entry while deduplicating by player id."""
    if not key:
        return
    lookup.setdefault(key, {})
    lookup[key][entry.player_id] = entry


def _select_unique_match(
    lookup: dict[str, dict[int, _LookupEntry]],
    key: str,
) -> tuple[Optional[PlayerMatch], bool]:
    """Return a unique normalized match or flag ambiguity for a lookup key."""
    entries = lookup.get(key, {})
    if not entries:
        return None, False
    if len(entries) > 1:
        return None, True

    entry = next(iter(entries.values()))
    return (
        PlayerMatch(
            player_id=entry.player_id,
            display_name=entry.display_name,
            matched_via=entry.matched_via,
        ),
        False,
    )


async def _build_player_name_lookup(db: AsyncSession) -> _PlayerNameLookup:
    """Load display and alias names into normalized exact and relaxed maps."""
    display_exact: dict[str, dict[int, _LookupEntry]] = {}
    alias_exact: dict[str, dict[int, _LookupEntry]] = {}
    display_relaxed: dict[str, dict[int, _LookupEntry]] = {}
    alias_relaxed: dict[str, dict[int, _LookupEntry]] = {}

    player_rows = (
        await db.execute(
            select(PlayerMaster.id, PlayerMaster.display_name)  # type: ignore[call-overload]
        )
    ).all()
    for player_id, display_name in player_rows:
        if player_id is None or not display_name:
            continue
        entry = _LookupEntry(
            player_id=player_id,
            display_name=display_name,
            matched_via="display_name",
        )
        _add_lookup_entry(display_exact, _normalized_name_key(display_name), entry)
        _add_lookup_entry(
            display_relaxed,
            _normalized_name_key(
                display_name,
                ignore_suffix=True,
                ignore_middle_initials=True,
            ),
            entry,
        )

    alias_rows = (
        await db.execute(
            select(
                PlayerAlias.player_id, PlayerAlias.full_name, PlayerMaster.display_name
            ).join(PlayerMaster, PlayerMaster.id == PlayerAlias.player_id)  # type: ignore[call-overload]  # type: ignore[arg-type]
        )
    ).all()
    for player_id, full_name, display_name in alias_rows:
        if player_id is None or not full_name:
            continue
        entry = _LookupEntry(
            player_id=player_id,
            display_name=display_name or full_name,
            matched_via="alias",
        )
        _add_lookup_entry(alias_exact, _normalized_name_key(full_name), entry)
        _add_lookup_entry(
            alias_relaxed,
            _normalized_name_key(
                full_name,
                ignore_suffix=True,
                ignore_middle_initials=True,
            ),
            entry,
        )

    return _PlayerNameLookup(
        display_exact=display_exact,
        alias_exact=alias_exact,
        display_relaxed=display_relaxed,
        alias_relaxed=alias_relaxed,
    )


def _resolve_from_lookup(
    lookup: _PlayerNameLookup,
    name: str,
) -> tuple[Optional[PlayerMatch], bool]:
    """Resolve a name via normalized exact and relaxed lookups."""
    exact_key = _normalized_name_key(name)
    relaxed_key = _normalized_name_key(
        name,
        ignore_suffix=True,
        ignore_middle_initials=True,
    )

    for candidate_lookup, candidate_key in (
        (lookup.display_exact, exact_key),
        (lookup.alias_exact, exact_key),
        (lookup.display_relaxed, relaxed_key),
        (lookup.alias_relaxed, relaxed_key),
    ):
        match, ambiguous = _select_unique_match(candidate_lookup, candidate_key)
        if match is not None:
            return match, False
        if ambiguous:
            return None, True

    return None, False


async def _insert_stub_alias(
    db: AsyncSession,
    player_id: int,
    full_name: str,
) -> None:
    """Persist the raw observed mention text as a player alias."""
    parsed = parse_player_name(full_name)
    db.add(
        PlayerAlias(
            player_id=player_id,
            full_name=full_name,
            first_name=parsed.first_name or None,
            middle_name=parsed.middle_name,
            last_name=parsed.last_name,
            suffix=parsed.suffix,
            context="mention_resolution",
        )
    )


async def _upsert_stub_lifecycle(
    db: AsyncSession,
    player_id: int,
    draft_year: Optional[int],
) -> None:
    """Ensure stub players get a lifecycle row without writing speculative facts."""
    result = await db.execute(
        select(PlayerLifecycle).where(
            PlayerLifecycle.player_id == player_id  # type: ignore[arg-type]
        )
    )
    lifecycle = result.scalar_one_or_none()
    if lifecycle is None:
        lifecycle = PlayerLifecycle(
            player_id=player_id,
            source="mention_resolution",
        )
        db.add(lifecycle)

    lifecycle.expected_draft_year = draft_year
    lifecycle.career_status = CareerStatus.PROSPECT
    lifecycle.draft_status = DraftStatus.UNKNOWN
    lifecycle.is_draft_prospect = True


async def _resolve_iter(
    db: AsyncSession,
    names: list[str],
    draft_year: Optional[int],
    create_stubs: bool,
) -> list[tuple[str, PlayerMatch]]:
    """Core matching loop shared by the public resolve functions.

    Returns:
        List of (input_name, PlayerMatch) tuples, deduplicated by player_id.
    """
    results: list[tuple[str, PlayerMatch]] = []
    seen_ids: set[int] = set()
    lookup = await _build_player_name_lookup(db)

    for raw_name in names:
        name = raw_name.strip()
        if not name:
            continue

        match, ambiguous = _resolve_from_lookup(lookup, name)
        if match is None and ambiguous:
            logger.warning(
                "Skipping ambiguous player mention without stub creation: %s",
                name,
            )
            continue
        if match is None and create_stubs:
            if not _can_create_stub_player(name):
                logger.info(
                    "Skipping low-specificity player mention without stub creation: %s",
                    name,
                )
                continue

            match = await _create_stub_player(db, name, draft_year=draft_year)
            if match is not None:
                alias_display_name = match.display_name
                stub_entry = _LookupEntry(
                    player_id=match.player_id,
                    display_name=alias_display_name,
                    matched_via="display_name",
                )
                _add_lookup_entry(
                    lookup.display_exact,
                    _normalized_name_key(alias_display_name),
                    stub_entry,
                )
                _add_lookup_entry(
                    lookup.display_relaxed,
                    _normalized_name_key(
                        alias_display_name,
                        ignore_suffix=True,
                        ignore_middle_initials=True,
                    ),
                    stub_entry,
                )
                _add_lookup_entry(
                    lookup.alias_exact,
                    _normalized_name_key(name),
                    _LookupEntry(
                        player_id=match.player_id,
                        display_name=alias_display_name,
                        matched_via="alias",
                    ),
                )
                _add_lookup_entry(
                    lookup.alias_relaxed,
                    _normalized_name_key(
                        name,
                        ignore_suffix=True,
                        ignore_middle_initials=True,
                    ),
                    _LookupEntry(
                        player_id=match.player_id,
                        display_name=alias_display_name,
                        matched_via="alias",
                    ),
                )

        if match is not None and match.player_id not in seen_ids:
            seen_ids.add(match.player_id)
            results.append((name, match))

    return results


async def resolve_player_names(
    db: AsyncSession,
    names: list[str],
    draft_year: Optional[int] = None,
    create_stubs: bool = True,
) -> list[PlayerMatch]:
    """Resolve a list of player name strings to database records.

    For each name, checks in order:
      1. Normalized PlayerMaster.display_name match
      2. Normalized PlayerAlias.full_name match
      3. Relaxed normalized match that ignores suffixes and middle initials
      4. Creates a stub PlayerMaster if create_stubs=True and the lookup
         is not ambiguous

    Args:
        db: Async database session (caller manages transaction)
        names: List of player name strings to resolve
        draft_year: Optional draft year to set on created stubs
        create_stubs: Whether to create stub records for unmatched names

    Returns:
        List of PlayerMatch results (one per successfully resolved name)
    """
    if not names:
        return []
    pairs = await _resolve_iter(db, names, draft_year, create_stubs)
    return [match for _, match in pairs]


async def resolve_player_names_as_map(
    db: AsyncSession,
    names: list[str],
    draft_year: Optional[int] = None,
    create_stubs: bool = True,
) -> dict[str, int]:
    """Resolve player name strings to a map of input_name -> player_id.

    Unlike resolve_player_names(), this maps each *input* name (lowered) to
    its resolved player_id, ensuring alias-matched names are correctly keyed
    by the caller's original string rather than the canonical display_name.

    Args:
        db: Async database session (caller manages transaction)
        names: List of player name strings to resolve
        draft_year: Optional draft year to set on created stubs
        create_stubs: Whether to create stub records for unmatched names

    Returns:
        Dict mapping lowered input name -> player_id
    """
    if not names:
        return {}
    pairs = await _resolve_iter(db, names, draft_year, create_stubs)
    return {input_name.lower(): match.player_id for input_name, match in pairs}


async def _create_stub_player(
    db: AsyncSession,
    full_name: str,
    draft_year: Optional[int] = None,
) -> Optional[PlayerMatch]:
    """Create a minimal stub PlayerMaster record for an unknown player.

    Args:
        db: Async database session
        full_name: Full player name
        draft_year: Optional draft year

    Returns:
        PlayerMatch if created successfully, None otherwise
    """
    parsed = parse_player_name(full_name)
    display_name = _collapse_whitespace(full_name)

    player = PlayerMaster(
        first_name=parsed.first_name or None,
        middle_name=parsed.middle_name,
        last_name=parsed.last_name,
        suffix=parsed.suffix,
        display_name=display_name,
        is_stub=True,
    )
    db.add(player)
    await db.flush()
    await _insert_stub_alias(db, player.id, display_name)  # type: ignore[arg-type]
    await _upsert_stub_lifecycle(db, player.id, draft_year)  # type: ignore[arg-type]
    logger.info(f"Created stub player: {display_name} (id={player.id})")
    return PlayerMatch(
        player_id=player.id,  # type: ignore[arg-type]
        display_name=display_name,
        matched_via="stub_created",
    )
