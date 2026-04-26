"""Canonical affiliation and player identity resolution helpers."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHOOL_MAPPING_PATH = REPO_ROOT / "scripts" / "data" / "school_mapping.json"
DEFAULT_COLLEGE_SCHOOLS_PATH = REPO_ROOT / "scripts" / "data" / "college_schools.json"

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

_SUFFIXES = {"jr", "junior", "sr", "senior", "ii", "iii", "iv", "v", "vi"}


@dataclass(frozen=True, slots=True)
class AffiliationResolution:
    """Resolved form of a raw school/team/club value."""

    raw_affiliation: str
    canonical_affiliation: str
    affiliation_type: str
    resolution_status: str
    review_note: str = ""

    @property
    def is_mapped(self) -> bool:
        """Return whether this raw affiliation has an intentional resolution."""
        return self.resolution_status != "needs_review"


def _load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def load_school_mapping(
    path: Path = DEFAULT_SCHOOL_MAPPING_PATH,
) -> dict[str, str | None]:
    """Load the reviewed raw-affiliation to canonical-affiliation mapping."""
    mapping = _load_json(path)
    if not isinstance(mapping, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return {
        str(raw): None if canonical is None else str(canonical)
        for raw, canonical in mapping.items()
    }


def load_college_school_names(path: Path = DEFAULT_COLLEGE_SCHOOLS_PATH) -> set[str]:
    """Load canonical college school names."""
    rows = _load_json(path)
    if not isinstance(rows, list):
        raise TypeError(f"{path} must contain a JSON array")
    return {str(row["name"]) for row in rows if isinstance(row, dict) and "name" in row}


def _ascii_fold(value: str) -> str:
    """Fold Unicode text to a punctuation-normalized ASCII representation."""
    normalized = unicodedata.normalize("NFKD", value.translate(_PUNCT_TRANSLATION))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _normalized_token(token: str) -> str:
    """Normalize a token for player-name comparison."""
    return re.sub(r"[^a-z0-9]", "", _ascii_fold(token).lower())


def _is_suffix(token: str) -> bool:
    """Return whether a token is a recognized suffix."""
    return _normalized_token(token) in _SUFFIXES


def normalize_player_name(
    full_name: str,
    *,
    ignore_suffix: bool = True,
    ignore_middle_initials: bool = True,
) -> str:
    """Build a normalized key for player identity matching.

    By default this is intentionally suffix-insensitive so variants like
    ``Darius Acuff`` and ``Darius Acuff Jr.`` resolve to the same review key.
    """
    raw_tokens = re.sub(r"\s+", " ", full_name.strip()).split()
    if raw_tokens and _is_suffix(raw_tokens[-1]):
        suffix = raw_tokens.pop()
    else:
        suffix = ""

    tokens = [_normalized_token(token) for token in raw_tokens]
    tokens = [token for token in tokens if token]
    if ignore_middle_initials and len(tokens) > 2:
        tokens = [
            tokens[0],
            *[token for token in tokens[1:-1] if len(token) > 1],
            tokens[-1],
        ]
    if suffix and not ignore_suffix:
        tokens.append(_normalized_token(suffix))
    return " ".join(tokens)


def resolve_affiliation(
    raw_affiliation: str,
    mapping: dict[str, str | None],
    college_school_names: set[str],
) -> AffiliationResolution:
    """Resolve a raw source affiliation into canonical review fields."""
    normalized_raw = raw_affiliation.translate(_PUNCT_TRANSLATION)
    if raw_affiliation in mapping:
        canonical = mapping[raw_affiliation]
        if canonical is None:
            return AffiliationResolution(
                raw_affiliation=raw_affiliation,
                canonical_affiliation="",
                affiliation_type="professional_or_international",
                resolution_status="mapped_intentional_non_college",
            )
        return AffiliationResolution(
            raw_affiliation=raw_affiliation,
            canonical_affiliation=canonical,
            affiliation_type="college",
            resolution_status="mapped",
        )
    if normalized_raw in mapping:
        canonical = mapping[normalized_raw]
        if canonical is None:
            return AffiliationResolution(
                raw_affiliation=raw_affiliation,
                canonical_affiliation="",
                affiliation_type="professional_or_international",
                resolution_status="mapped_intentional_non_college",
            )
        return AffiliationResolution(
            raw_affiliation=raw_affiliation,
            canonical_affiliation=canonical,
            affiliation_type="college",
            resolution_status="mapped_punctuation_normalized",
        )
    if raw_affiliation in college_school_names:
        return AffiliationResolution(
            raw_affiliation=raw_affiliation,
            canonical_affiliation=raw_affiliation,
            affiliation_type="college",
            resolution_status="canonical_school_name",
        )
    if normalized_raw in college_school_names:
        return AffiliationResolution(
            raw_affiliation=raw_affiliation,
            canonical_affiliation=normalized_raw,
            affiliation_type="college",
            resolution_status="canonical_school_name_punctuation_normalized",
        )
    return AffiliationResolution(
        raw_affiliation=raw_affiliation,
        canonical_affiliation="",
        affiliation_type="unknown",
        resolution_status="needs_review",
        review_note="Add raw affiliation to school_mapping.json",
    )
