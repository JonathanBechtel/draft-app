"""Unit tests for the consensus board's position-label formatter."""

from app.services.consensus_read_service import _format_position


class TestFormatPosition:
    def test_code_is_uppercased_with_slash(self) -> None:
        """A structured position code renders as uppercase with slash separators."""
        assert _format_position("pg_sg", None) == "PG/SG"
        assert _format_position("pf_c", None) == "PF/C"

    def test_hyphenated_code_normalizes_to_slash(self) -> None:
        """Hybrid codes seeded with hyphens (pg-sg) also render with a slash."""
        assert _format_position("pg-sg", None) == "PG/SG"
        assert _format_position("pf-c", None) == "PF/C"

    def test_single_code(self) -> None:
        """A single-token code uppercases cleanly."""
        assert _format_position("c", None) == "C"
        assert _format_position("sf", None) == "SF"

    def test_code_takes_precedence_over_raw(self) -> None:
        """When both are present the structured code wins."""
        assert _format_position("pg", "Guard") == "PG"

    def test_raw_word_is_abbreviated(self) -> None:
        """Free-text position words fall back to a single-letter abbreviation."""
        assert _format_position(None, "Guard") == "G"
        assert _format_position(None, "Forward") == "F"
        assert _format_position(None, "Center") == "C"

    def test_unknown_raw_passes_through(self) -> None:
        """An unrecognized raw position is returned as-is (trimmed)."""
        assert _format_position(None, " Combo ") == "Combo"

    def test_none_when_no_data(self) -> None:
        """No code and no raw position yields None."""
        assert _format_position(None, None) is None
        assert _format_position("", "") is None
