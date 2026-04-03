"""Unit tests for player mention normalization and name parsing."""

from app.services.player_mention_service import (
    _normalized_name_key,
    parse_player_name,
    split_name,
)


class TestSplitName:
    """Tests for the split_name utility function."""

    def test_two_part_name(self) -> None:
        """Standard first-last name should split correctly."""
        first, last = split_name("Cooper Flagg")
        assert first == "Cooper"
        assert last == "Flagg"

    def test_suffix_is_excluded_from_last_name(self) -> None:
        """Recognized suffixes should not be folded into last_name."""
        first, last = split_name("Lamine Camara Jr")
        assert first == "Lamine"
        assert last == "Camara"

    def test_single_word_name(self) -> None:
        """Single name should return first_name with no last_name."""
        first, last = split_name("Nene")
        assert first == "Nene"
        assert last is None

    def test_empty_string(self) -> None:
        """Empty string should return empty first_name and None last."""
        first, last = split_name("")
        assert first == ""
        assert last is None

    def test_whitespace_only(self) -> None:
        """Whitespace-only should return empty first_name and None last."""
        first, last = split_name("   ")
        assert first == ""
        assert last is None

    def test_leading_trailing_whitespace(self) -> None:
        """Leading/trailing whitespace should be stripped."""
        first, last = split_name("  Ace Bailey  ")
        assert first == "Ace"
        assert last == "Bailey"

    def test_extra_internal_whitespace(self) -> None:
        """Extra internal whitespace should be collapsed by split."""
        first, last = split_name("VJ  Edgecombe")
        assert first == "VJ"
        assert last == "Edgecombe"


class TestParsePlayerName:
    """Tests for structured parsing of freeform player names."""

    def test_parses_middle_and_suffix(self) -> None:
        """Parser should separate middle names and recognized suffixes."""
        parsed = parse_player_name("Walter A. Clayton Jr")
        assert parsed.first_name == "Walter"
        assert parsed.middle_name == "A."
        assert parsed.last_name == "Clayton"
        assert parsed.suffix == "Jr."

    def test_unrecognized_suffix_stays_in_last_name(self) -> None:
        """Unknown trailing tokens should remain part of the last name."""
        parsed = parse_player_name("Player Example XYZ")
        assert parsed.first_name == "Player"
        assert parsed.middle_name == "Example"
        assert parsed.last_name == "XYZ"
        assert parsed.suffix is None


class TestNormalizedNameKey:
    """Tests for exact and relaxed normalized name keys."""

    def test_exact_key_normalizes_punctuation(self) -> None:
        """Exact keys should normalize punctuation-only differences."""
        assert _normalized_name_key("D.J. Harper") == _normalized_name_key(
            "DJ Harper"
        )

    def test_exact_key_normalizes_suffix_spelling(self) -> None:
        """Exact keys should canonicalize equivalent suffix spellings."""
        assert _normalized_name_key("Darius Acuff Jr") == _normalized_name_key(
            "Darius Acuff Junior"
        )

    def test_relaxed_key_ignores_suffix_and_middle_initial(self) -> None:
        """Relaxed keys should collapse optional suffixes and initials."""
        assert _normalized_name_key(
            "Walter A. Clayton Jr",
            ignore_suffix=True,
            ignore_middle_initials=True,
        ) == _normalized_name_key(
            "Walter Clayton",
            ignore_suffix=True,
            ignore_middle_initials=True,
        )

    def test_relaxed_key_normalizes_unicode_punctuation(self) -> None:
        """Relaxed keys should normalize straight and curly apostrophes."""
        assert _normalized_name_key(
            "Day'Ron Sharpe",
            ignore_suffix=True,
            ignore_middle_initials=True,
        ) == _normalized_name_key(
            "Day’Ron Sharpe",
            ignore_suffix=True,
            ignore_middle_initials=True,
        )
