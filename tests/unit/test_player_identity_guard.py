"""Variant-aware canonical identity matching and duplicate-audit behavior."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.player_identity_guard import (
    audit_variant_player_duplicates,
    find_variant_identity_matches,
    normalize_player_identity_name,
)


class _FakeResult:
    """Small SQLAlchemy result stand-in returning prepared rows."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        """Return the prepared rows."""
        return self._rows


class _FakeDb:
    """Return one prepared result per execute call."""

    def __init__(self, results: list[list[tuple[Any, ...]]]) -> None:
        self._results = list(results)

    async def execute(self, statement: object) -> _FakeResult:
        """Return the next result without evaluating ``statement``."""
        del statement
        return _FakeResult(self._results.pop(0))


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        (" José   García Jr. ", "jose garcia"),
        ("P.J. Washington", "pj washington"),
        ("Jean-Luc O’Neal III", "jean luc oneal"),
        ("Salaün", "salaun"),
    ],
)
def test_normalize_player_identity_name_folds_supported_variants(
    raw_name: str,
    expected: str,
) -> None:
    """Suffix, diacritic, apostrophe, period, and hyphen variants share keys."""
    assert normalize_player_identity_name(raw_name) == expected


@pytest.mark.asyncio
async def test_find_variant_identity_matches_uses_aliases_without_display_match() -> None:
    """Alias variants resolve when no canonical display variant exists."""
    db = _FakeDb(
        [
            [
                (1, "Paul Washington"),
                (2, "Unrelated Player"),
            ],
            [
                (2, "Unrelated Player", "PJ Washington"),
                (1, "P.J. Washington Jr.", "Paul Washington"),
            ],
        ]
    )

    matches = await find_variant_identity_matches(db, "PJ Washington")  # type: ignore[arg-type]

    assert matches.player_ids == frozenset({2})
    assert matches.display_names == {}
    assert matches.alias_names == {2: "Unrelated Player"}


@pytest.mark.asyncio
async def test_find_variant_identity_matches_unions_display_and_alias_conflicts() -> None:
    """A display variant stays ambiguous when another player's alias collides."""
    db = _FakeDb(
        [
            [(1, "P.J. Washington Jr."), (2, "Unrelated Player")],
            [(2, "Unrelated Player", "PJ Washington")],
        ]
    )

    matches = await find_variant_identity_matches(db, "PJ Washington")  # type: ignore[arg-type]

    assert matches.player_ids == frozenset({1, 2})
    assert matches.alias_names == {2: "Unrelated Player"}


@pytest.mark.asyncio
async def test_duplicate_audit_classifies_likely_duplicates_and_namesakes() -> None:
    """The recurring audit surfaces empty stubs but does not accuse identified pairs."""
    db = _FakeDb(
        [
            [
                (1, "Salaün Jr.", False, 2026),
                (2, "Salaun", True, 2026),
                (3, "Gary Payton", False, 1986),
                (4, "Gary Payton II", False, 2016),
                (5, "Unique Prospect", False, 2026),
            ],
            [(1,), (3,), (4,)],
        ]
    )

    report = await audit_variant_player_duplicates(db)  # type: ignore[arg-type]

    assert report.likely_duplicate_count == 1
    assert [
        (group.normalized_name, group.classification) for group in report.groups
    ] == [
        ("gary payton", "identified_namesakes"),
        ("salaun", "likely_duplicate"),
    ]
