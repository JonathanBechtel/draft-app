"""Variant-aware canonical player identity matching and duplicate auditing."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.players_master import PlayerMaster

_NAME_SUFFIX_RE = re.compile(
    r"\s+(jr|junior|sr|senior|ii|iii|iv|v)\s*$",
    re.IGNORECASE,
)
_JOINING_PUNCTUATION = frozenset({"'", "’", "ʼ", "`", "."})
_SUFFIX_CANONICAL = {
    "jr": "jr",
    "junior": "jr",
    "sr": "sr",
    "senior": "sr",
    "ii": "ii",
    "iii": "iii",
    "iv": "iv",
    "v": "v",
}


def normalize_player_identity_name(name: str) -> str:
    """Return a suffix-, diacritic-, and punctuation-insensitive identity key."""
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name)
    normalized: list[str] = []
    for character in folded:
        if unicodedata.combining(character):
            continue
        if character in _JOINING_PUNCTUATION:
            continue
        if unicodedata.category(character).startswith("P"):
            normalized.append(" ")
            continue
        normalized.append(character)
    without_suffix = _NAME_SUFFIX_RE.sub("", "".join(normalized))
    return re.sub(r"\s+", " ", without_suffix.strip()).casefold()


def player_identity_suffix(name: str) -> str | None:
    """Return the canonical recognized suffix carried by ``name``."""
    if not name or not name.strip():
        return None
    folded = unicodedata.normalize("NFKD", name.strip().split()[-1])
    token = "".join(
        character for character in folded if not unicodedata.combining(character)
    )
    token = re.sub(r"[^a-z0-9]", "", token.casefold())
    return _SUFFIX_CANONICAL.get(token)


def identity_suffixes_differ(source_name: str, candidate_name: str | None) -> bool:
    """Return whether two names carry different recognized suffixes."""
    return player_identity_suffix(source_name) != player_identity_suffix(
        candidate_name or ""
    )


@dataclass(frozen=True, slots=True)
class IdentityVariantMatches:
    """Canonical players matching one normalized display name or alias."""

    display_names: dict[int, str | None]
    alias_names: dict[int, str | None]
    alias_match_names: dict[int, tuple[str, ...]] = field(default_factory=dict)

    @property
    def player_ids(self) -> frozenset[int]:
        """Return distinct canonical player IDs across both match sources."""
        return frozenset(self.display_names) | frozenset(self.alias_names)

    def display_name_for(self, player_id: int) -> str | None:
        """Return the canonical display name known for ``player_id``."""
        return self.display_names.get(player_id) or self.alias_names.get(player_id)

    def match_names_for(self, player_id: int) -> tuple[str, ...]:
        """Return canonical and explicit alias spellings for suffix comparison."""
        names: list[str] = []
        display_name = self.display_name_for(player_id)
        if display_name:
            names.append(display_name)
        names.extend(self.alias_match_names.get(player_id, ()))
        return tuple(names)


@dataclass(slots=True)
class IdentityVariantIndex:
    """In-memory index of display and alias names for one ingest run."""

    display_names_by_key: dict[str, dict[int, str | None]] = field(default_factory=dict)
    alias_names_by_key: dict[str, dict[int, str | None]] = field(default_factory=dict)
    alias_match_names_by_key: dict[str, dict[int, tuple[str, ...]]] = field(
        default_factory=dict
    )

    def matches_for(self, source_name: str) -> IdentityVariantMatches:
        """Return all canonical rows matching ``source_name``'s variant key."""
        needle = normalize_player_identity_name(source_name)
        return IdentityVariantMatches(
            display_names=dict(self.display_names_by_key.get(needle, {})),
            alias_names=dict(self.alias_names_by_key.get(needle, {})),
            alias_match_names=dict(self.alias_match_names_by_key.get(needle, {})),
        )

    def add_display_name(self, player_id: int, display_name: str | None) -> None:
        """Add a newly inserted canonical display name to the run index."""
        if not display_name:
            return
        key = normalize_player_identity_name(display_name)
        if not key:
            return
        self.display_names_by_key.setdefault(key, {})[player_id] = display_name


IdentityMatchStatus = Literal["none", "exact", "alias", "ambiguous", "suffix_mismatch"]


@dataclass(frozen=True, slots=True)
class IdentityMatchResolution:
    """Safe action for one variant-normalized identity lookup."""

    status: IdentityMatchStatus
    player_id: int | None = None
    display_name: str | None = None
    candidate_ids: tuple[int, ...] = ()


def resolve_variant_identity_match(
    source_name: str,
    matches: IdentityVariantMatches,
) -> IdentityMatchResolution:
    """Classify a normalized match without guessing across suffixes."""
    player_ids = matches.player_ids
    if not player_ids:
        return IdentityMatchResolution(status="none")
    if len(player_ids) > 1:
        return IdentityMatchResolution(
            status="ambiguous",
            candidate_ids=tuple(sorted(player_ids)),
        )

    player_id = next(iter(player_ids))
    match_names = matches.match_names_for(player_id)
    if match_names and all(
        identity_suffixes_differ(source_name, match_name) for match_name in match_names
    ):
        return IdentityMatchResolution(
            status="suffix_mismatch",
            player_id=player_id,
            display_name=matches.display_name_for(player_id),
            candidate_ids=(player_id,),
        )

    return IdentityMatchResolution(
        status="exact" if player_id in matches.display_names else "alias",
        player_id=player_id,
        display_name=matches.display_name_for(player_id),
        candidate_ids=(player_id,),
    )


def _build_variant_identity_index(
    display_rows: Sequence[Any],
    alias_rows: Sequence[Any],
) -> IdentityVariantIndex:
    """Build the variant index from already-fetched database rows."""
    index = IdentityVariantIndex()
    for player_id, display_name in display_rows:
        if player_id is None or not display_name:
            continue
        key = normalize_player_identity_name(str(display_name))
        if key:
            index.display_names_by_key.setdefault(key, {})[int(player_id)] = str(
                display_name
            )

    alias_names: dict[str, dict[int, str | None]] = {}
    alias_match_names: dict[str, dict[int, list[str]]] = {}
    for player_id, display_name, alias_name in alias_rows:
        if player_id is None or not alias_name:
            continue
        key = normalize_player_identity_name(str(alias_name))
        if not key:
            continue
        canonical_id = int(player_id)
        alias_names.setdefault(key, {})[canonical_id] = (
            str(display_name) if display_name else None
        )
        alias_match_names.setdefault(key, {}).setdefault(canonical_id, []).append(
            str(alias_name)
        )

    index.alias_names_by_key = alias_names
    index.alias_match_names_by_key = {
        key: {player_id: tuple(names) for player_id, names in player_names.items()}
        for key, player_names in alias_match_names.items()
    }
    return index


async def build_variant_identity_index(db: AsyncSession) -> IdentityVariantIndex:
    """Load display and alias identities once for a batch ingest."""
    display_rows = (
        await db.execute(
            select(PlayerMaster.id, PlayerMaster.display_name)  # type: ignore[call-overload]
        )
    ).all()
    alias_rows = (
        await db.execute(
            select(  # type: ignore[call-overload]
                PlayerAlias.player_id,
                PlayerMaster.display_name,
                PlayerAlias.full_name,
            ).join(PlayerMaster, PlayerMaster.id == PlayerAlias.player_id)
        )
    ).all()
    return _build_variant_identity_index(display_rows, alias_rows)


async def find_variant_identity_matches(
    db: AsyncSession,
    source_name: str,
    *,
    index: IdentityVariantIndex | None = None,
) -> IdentityVariantMatches:
    """Find every canonical player sharing a variant-aware identity key."""
    if index is None:
        index = await build_variant_identity_index(db)
    return index.matches_for(source_name)


@dataclass(frozen=True, slots=True)
class IdentityDuplicateMember:
    """One canonical row in a normalized-name collision group."""

    player_id: int
    display_name: str
    is_stub: bool
    draft_year: int | None
    external_id_count: int


@dataclass(frozen=True, slots=True)
class IdentityDuplicateGroup:
    """One reviewable group of canonical rows sharing a normalized name."""

    normalized_name: str
    classification: str
    members: tuple[IdentityDuplicateMember, ...]


@dataclass(frozen=True, slots=True)
class IdentityDuplicateAuditReport:
    """Read-only result of the recurring canonical-player duplicate audit."""

    groups: tuple[IdentityDuplicateGroup, ...]

    @property
    def likely_duplicate_count(self) -> int:
        """Return groups whose evidence matches the safe duplicate shape."""
        return sum(group.classification == "likely_duplicate" for group in self.groups)


def _classify_duplicate_group(
    members: tuple[IdentityDuplicateMember, ...],
) -> str:
    """Classify a collision without ever deciding that rows should be merged."""
    if all(member.external_id_count > 0 for member in members):
        return "identified_namesakes"
    if len(members) != 2:
        return "review"

    empty_stubs = [
        member for member in members if member.is_stub and member.external_id_count == 0
    ]
    identified = [
        member
        for member in members
        if not member.is_stub and member.external_id_count > 0
    ]
    known_years = {
        member.draft_year for member in members if member.draft_year is not None
    }
    if len(empty_stubs) == 1 and len(identified) == 1 and len(known_years) <= 1:
        return "likely_duplicate"
    return "review"


async def audit_variant_player_duplicates(
    db: AsyncSession,
) -> IdentityDuplicateAuditReport:
    """Report canonical player rows colliding under variant normalization."""
    player_rows = (
        await db.execute(
            select(  # type: ignore[call-overload]
                PlayerMaster.id,
                PlayerMaster.display_name,
                PlayerMaster.is_stub,
                PlayerMaster.draft_year,
            )
        )
    ).all()
    external_rows = (
        await db.execute(
            select(PlayerExternalId.player_id)  # type: ignore[call-overload]
        )
    ).all()
    external_counts: dict[int, int] = {}
    for (player_id,) in external_rows:
        canonical_id = int(player_id)
        external_counts[canonical_id] = external_counts.get(canonical_id, 0) + 1

    by_key: dict[str, list[IdentityDuplicateMember]] = {}
    for player_id, display_name, is_stub, draft_year in player_rows:
        if not display_name:
            continue
        normalized_name = normalize_player_identity_name(str(display_name))
        if not normalized_name:
            continue
        canonical_id = int(player_id)
        by_key.setdefault(normalized_name, []).append(
            IdentityDuplicateMember(
                player_id=canonical_id,
                display_name=str(display_name),
                is_stub=bool(is_stub),
                draft_year=int(draft_year) if draft_year is not None else None,
                external_id_count=external_counts.get(canonical_id, 0),
            )
        )

    groups: list[IdentityDuplicateGroup] = []
    for normalized_name, raw_members in sorted(by_key.items()):
        if len(raw_members) < 2:
            continue
        members = tuple(sorted(raw_members, key=lambda member: member.player_id))
        groups.append(
            IdentityDuplicateGroup(
                normalized_name=normalized_name,
                classification=_classify_duplicate_group(members),
                members=members,
            )
        )
    return IdentityDuplicateAuditReport(groups=tuple(groups))
