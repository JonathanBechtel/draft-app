"""Tests for the Top 100 refresh artifact generator."""

from scripts import top100_refresh


def test_top100_source_data_contains_exactly_100_rows() -> None:
    """The frozen source data should cover exactly the selected Top 100 board."""
    assert len(top100_refresh.TOP100_ROWS) == 100
    assert top100_refresh.TOP100_ROWS[0].source_rank == 1
    assert top100_refresh.TOP100_ROWS[-1].source_rank == 100


def test_name_normalization_handles_suffix_and_punctuation_variants() -> None:
    """Suffix and apostrophe variants should share a player-resolution key."""
    assert top100_refresh.normalize_name("Darius Acuff Jr.") == "darius acuff"
    assert top100_refresh.normalize_name("Darius Acuff") == "darius acuff"
    assert top100_refresh.normalize_name("Ja’Kobi Gillespie") == "jakobi gillespie"


def test_affiliation_resolution_handles_known_variants() -> None:
    """Top 100 raw affiliations should resolve known school and pro variants."""
    mapping = top100_refresh.load_school_mapping()
    schools = top100_refresh.load_college_school_names()

    unc = top100_refresh.resolve_affiliation("North Carolina", mapping, schools)
    uconn = top100_refresh.resolve_affiliation("Connecticut", mapping, schools)
    breakers = top100_refresh.resolve_affiliation("NZ Breakers", mapping, schools)

    assert unc == ("UNC", "college", "mapped", "")
    assert uconn == ("UConn", "college", "mapped", "")
    assert breakers == (
        "",
        "professional_or_international",
        "mapped_intentional_non_college",
        "",
    )
