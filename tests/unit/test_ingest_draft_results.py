"""Unit tests for the draft-results ingestion line parser.

The parser turns a pasted ``pick name TEAM`` block into structured picks; it
must tolerate messy real-world paste (numbering punctuation, missing team
tokens, multi-word names, stray header lines) without a DB.
"""

from __future__ import annotations

from scripts.ingest_draft_results import (
    _DEFAULT_DRAFT_YEAR,
    _resolve_draft_year,
    parse_lines,
)


def test_parses_basic_pick_name_team_lines() -> None:
    """A clean block yields one ParsedPick per line with team split off."""
    text = "1  Cooper Flagg  DAL\n2  Dylan Harper  SAS\n3  VJ Edgecombe  PHI\n"
    picks = parse_lines(text)
    assert [(p.overall_pick, p.player_name, p.team_abbr) for p in picks] == [
        (1, "Cooper Flagg", "DAL"),
        (2, "Dylan Harper", "SAS"),
        (3, "VJ Edgecombe", "PHI"),
    ]


def test_tolerates_numbering_punctuation_and_headers() -> None:
    """Leading '#'/'.' numbering and non-pick header lines are handled."""
    text = "ROUND 1\n#1. Cooper Flagg DAL\n10) Khaman Maluach UTA\n"
    picks = parse_lines(text)
    assert [(p.overall_pick, p.player_name, p.team_abbr) for p in picks] == [
        (1, "Cooper Flagg", "DAL"),
        (10, "Khaman Maluach", "UTA"),
    ]


def test_missing_team_token_leaves_team_none() -> None:
    """A line without a trailing all-caps team token still parses."""
    picks = parse_lines("5 Ace Bailey\n")
    assert len(picks) == 1
    assert picks[0].team_abbr is None
    assert picks[0].player_name == "Ace Bailey"


def test_blank_and_garbage_lines_skipped() -> None:
    """Empty lines and lines without a leading pick number are dropped."""
    picks = parse_lines("\n\nsome commentary\n2 Dylan Harper SAS\n")
    assert len(picks) == 1
    assert picks[0].overall_pick == 2


def test_comment_lines_skipped() -> None:
    """Lines starting with '#' are comments — even when a number follows.

    The canonical data files carry a '# 2026 NBA Draft ...' header; without
    comment-skipping the leading year would be misread as pick #2026.
    """
    text = "# 2026 NBA Draft — Round 1\n# Format: pick name TEAM\n1 Cooper Flagg DAL\n"
    picks = parse_lines(text)
    assert [(p.overall_pick, p.player_name) for p in picks] == [(1, "Cooper Flagg")]


def test_tab_separated_lines_parse() -> None:
    """The canonical file is tab-separated; team is still the trailing token."""
    picks = parse_lines("1\tAJ Dybantsa\tWAS\n")
    assert picks[0].overall_pick == 1
    assert picks[0].player_name == "AJ Dybantsa"
    assert picks[0].team_abbr == "WAS"


def test_resolve_draft_year_prefers_explicit_flag() -> None:
    """An explicit --draft-year always wins, even against a year-named file."""
    assert _resolve_draft_year(2025, "scripts/data/draft_results_2027.txt") == 2025


def test_resolve_draft_year_infers_from_file_name() -> None:
    """With no flag, the year comes from a draft_results_<YYYY>.txt file name.

    This is what lets the deploy loop and future years ingest with no flag.
    """
    assert _resolve_draft_year(None, "scripts/data/draft_results_2027.txt") == 2027
    assert _resolve_draft_year(None, "/abs/path/draft_results_2030.txt") == 2030


def test_resolve_draft_year_falls_back_to_default() -> None:
    """Stdin input (no file) or an unrecognized name falls back to the default."""
    assert _resolve_draft_year(None, None) == _DEFAULT_DRAFT_YEAR
    assert _resolve_draft_year(None, "picks.txt") == _DEFAULT_DRAFT_YEAR
