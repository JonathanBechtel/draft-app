"""Unit tests for country-name normalization (app.utils.country).

Verifies that mixed ISO-2 codes, full names, and loose aliases collapse onto a
single canonical display name, and that the reverse ``country_variants`` mapping
covers every encoding a filter must match.
"""

from __future__ import annotations

import pytest

from app.utils.country import canonical_country, country_variants


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("US", "United States"),
        ("us", "United States"),
        ("USA", "United States"),
        ("U.S.", "United States"),
        ("United States", "United States"),
        ("United States of America", "United States"),
        ("AU", "Australia"),
        ("Australia", "Australia"),
        ("BE", "Belgium"),
        ("Belgium", "Belgium"),
        ("KR", "South Korea"),
        ("South Korea", "South Korea"),
        ("  AU  ", "Australia"),  # whitespace trimmed
    ],
)
def test_canonical_country_collapses_encodings(raw: str, expected: str) -> None:
    """ISO codes, full names, and aliases all normalize to one canonical name."""
    assert canonical_country(raw) == expected


def test_canonical_country_blank_and_none() -> None:
    """Blank / None input returns None (no phantom facet entry)."""
    assert canonical_country(None) is None
    assert canonical_country("") is None
    assert canonical_country("   ") is None


def test_canonical_country_unknown_passthrough() -> None:
    """Unknown values survive (trimmed) rather than being dropped."""
    assert canonical_country("Atlantis") == "Atlantis"
    # Unknown 2-letter code is upper-cased but preserved.
    assert canonical_country("zz") == "ZZ"


def test_country_variants_covers_all_encodings() -> None:
    """country_variants returns canonical + code + alias forms for filtering."""
    variants = country_variants("United States")
    assert {"United States", "US", "USA"}.issubset(variants)
    # Every variant normalizes back to the same canonical name.
    for v in variants:
        assert canonical_country(v) == "United States"


def test_country_variants_includes_canonical_for_unmapped() -> None:
    """A country with no extra encodings still yields a singleton set."""
    assert country_variants("Atlantis") == {"Atlantis"}
