"""Unit tests for the player mention service name-splitting logic."""

from app.services.player_mention_service import split_name


class TestSplitName:
    """Tests for the split_name utility function."""

    def test_two_part_name(self) -> None:
        """Standard first-last name should split correctly."""
        first, last = split_name("Cooper Flagg")
        assert first == "Cooper"
        assert last == "Flagg"

    def test_three_part_name(self) -> None:
        """Multi-word last name should be preserved."""
        first, last = split_name("Lamine Camara Jr")
        assert first == "Lamine"
        assert last == "Camara Jr"

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
