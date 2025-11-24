from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Sequence


class PositionScopeKind(str, Enum):
    fine = "fine"
    parent = "parent"


_PARENT_ALIASES = {
    "guard": "guard",
    "guards": "guard",
    "g": "guard",
    "wing": "wing",
    "wings": "wing",
    "w": "wing",
    "forward": "forward",
    "forwards": "forward",
    "fwd": "forward",
    "f": "forward",
    "big": "big",
    "bigs": "big",
    "b": "big",
}

_BASE_ORDER = ["PG", "SG", "SF", "PF", "C", "G", "F"]
_BASE_NORMALIZATION = {
    "PG": "PG",
    "POINT": "PG",
    "POINTGUARD": "PG",
    "SG": "SG",
    "SHOOTING": "SG",
    "SHOOTINGGUARD": "SG",
    "SF": "SF",
    "SMALL": "SF",
    "SMALLFORWARD": "SF",
    "PF": "PF",
    "POWER": "PF",
    "POWERFORWARD": "PF",
    "C": "C",
    "CENTER": "C",
    "G": "G",
    "GUARD": "G",
    "F": "F",
    "FORWARD": "F",
}

_BASE_PARENT_MAP = {
    "PG": {"guard"},
    "SG": {"guard"},
    "G": {"guard"},
    "SF": {"wing", "forward"},
    "PF": {"forward", "big"},
    "F": {"forward"},
    "C": {"big"},
}

PARENT_SCOPE_PRESET: List[str] = ["guard", "wing", "forward", "big"]

FINE_SCOPE_PRESET: List[str] = [
    "pg",
    "sg",
    "sf",
    "pf",
    "c",
    "pg-sg",
    "sg-sf",
    "sf-pf",
    "pf-c",
]


def preset_scope_tokens(kind: str) -> List[str]:
    kind = kind.strip().lower()
    if kind == "parent":
        return PARENT_SCOPE_PRESET.copy()
    if kind == "fine":
        return FINE_SCOPE_PRESET.copy()
    raise ValueError(f"Unknown position matrix kind: {kind}")


@dataclass(frozen=True)
class PositionScope:
    kind: PositionScopeKind
    value: str


def _tokenize_raw_position(raw: str) -> List[str]:
    # Normalize delimiters: " and " -> "-", "/" -> "-"
    s = raw.replace(" and ", "-").replace("/", "-")
    # Remove remaining spaces and uppercase
    cleaned = s.replace(" ", "").upper()
    tokens = [tok for tok in cleaned.split("-") if tok]
    normalized: List[str] = []
    for token in tokens:
        canonical = _BASE_NORMALIZATION.get(token)
        if canonical is None:
            continue
        normalized.append(canonical)
    if not normalized:
        return []
    unique: List[str] = []
    for token in normalized:
        if token not in unique:
            unique.append(token)
    order_index = {code: idx for idx, code in enumerate(_BASE_ORDER)}
    unique.sort(key=lambda code: order_index.get(code, len(_BASE_ORDER)))
    return unique


def derive_position_tags(raw: Optional[str]) -> tuple[Optional[str], List[str]]:
    if raw is None:
        return None, []
    raw = raw.strip()
    if not raw:
        return None, []
    tokens = _tokenize_raw_position(raw)
    if not tokens:
        return None, []

    # Sort based on base order
    order_index = {code: i for i, code in enumerate(_BASE_ORDER)}
    unique = sorted(list(set(tokens)), key=lambda code: order_index.get(code, 99))

    fine_token = "_".join(token.lower() for token in unique)
    parents = get_parents_for_fine(fine_token.upper())
    return fine_token, parents


def resolve_position_scope(value: Optional[str]) -> Optional[PositionScope]:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    lower = candidate.lower()
    parent = _PARENT_ALIASES.get(lower)
    if parent:
        return PositionScope(kind=PositionScopeKind.parent, value=parent)
    fine, _ = derive_position_tags(candidate)
    if fine:
        return PositionScope(kind=PositionScopeKind.fine, value=fine)
    raise ValueError(f"Unknown position scope token: {value}")


def parents_for_scope(scope: PositionScope) -> Sequence[str]:
    if scope.kind == PositionScopeKind.parent:
        return [scope.value]
    fine_parents = get_parents_for_fine(scope.value)
    return fine_parents


def get_parents_for_fine(fine: Optional[str]) -> List[str]:
    if not fine:
        return []
    tokens = fine.split("_")
    parents: List[str] = []
    seen = set()
    for token in tokens:
        upper = token.upper()
        parent_set = _BASE_PARENT_MAP.get(upper, set())
        for parent in parent_set:
            if parent not in seen:
                parents.append(parent)
                seen.add(parent)
    return sorted(parents)
