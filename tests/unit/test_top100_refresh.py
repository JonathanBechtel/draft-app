"""Tests for the Top 100 refresh artifact generator."""

from app.services.canonical_resolution_service import normalize_player_name
from scripts.top100 import merge_players
from scripts.top100 import refresh


def test_top100_source_data_contains_exactly_100_rows() -> None:
    """The frozen source data should cover exactly the selected Top 100 board."""
    assert len(refresh.TOP100_ROWS) == 100
    assert refresh.TOP100_ROWS[0].source_rank == 1
    assert refresh.TOP100_ROWS[-1].source_rank == 100


def test_name_normalization_handles_suffix_and_punctuation_variants() -> None:
    """Suffix and apostrophe variants should share a player-resolution key."""
    assert normalize_player_name("Darius Acuff Jr.") == "darius acuff"
    assert normalize_player_name("Darius Acuff") == "darius acuff"
    assert normalize_player_name("Ja’Kobi Gillespie") == "jakobi gillespie"


def test_affiliation_resolution_handles_known_variants() -> None:
    """Top 100 raw affiliations should resolve known school and pro variants."""
    mapping = refresh.load_school_mapping()
    schools = refresh.load_college_school_names()

    unc = refresh.resolve_affiliation("North Carolina", mapping, schools)
    uconn = refresh.resolve_affiliation("Connecticut", mapping, schools)
    breakers = refresh.resolve_affiliation("NZ Breakers", mapping, schools)

    assert unc.canonical_affiliation == "UNC"
    assert unc.affiliation_type == "college"
    assert unc.resolution_status == "mapped"
    assert uconn.canonical_affiliation == "UConn"
    assert uconn.affiliation_type == "college"
    assert uconn.resolution_status == "mapped"
    assert breakers.canonical_affiliation == ""
    assert breakers.affiliation_type == "professional_or_international"
    assert breakers.resolution_status == "mapped_intentional_non_college"


def test_affiliation_resolution_flags_unknown_raw_school() -> None:
    """Unmapped school strings should be review output, not silent canonical values."""
    mapping = refresh.load_school_mapping()
    schools = refresh.load_college_school_names()

    result = refresh.resolve_affiliation("Totally Unknown Academy", mapping, schools)

    assert result.canonical_affiliation == ""
    assert result.affiliation_type == "unknown"
    assert result.resolution_status == "needs_review"


def test_top100_merge_plan_covers_duplicate_groups_once() -> None:
    """Reviewed merge plan should cover every Session 1 duplicate group."""
    plans = merge_players.MERGE_PLANS
    assert len(plans) == 9
    keep_ids = {plan.keep_id for plan in plans}
    discard_ids = {discard_id for plan in plans for discard_id in plan.discard_ids}

    assert 5384 in keep_ids  # Darius Acuff Jr. canonical row
    assert {5681, 6027}.issubset(discard_ids)
    assert keep_ids.isdisjoint(discard_ids)
