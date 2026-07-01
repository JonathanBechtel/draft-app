"""Unit tests for the Summer League roster reconcile CLI argument parsing."""

from __future__ import annotations

import pytest

from scripts import reconcile_summer_league_rosters as cli


def test_build_parser_accepts_year_and_league_id() -> None:
    """A single ``--league-id`` resolves to a one-element normalized list."""
    args = cli._build_parser().parse_args(["--year", "2025", "--league-id", "13"])

    assert args.year == 2025
    assert args.league_id == "13"
    assert args.all_venues is False


def test_build_parser_accepts_all_venues_flag() -> None:
    """``--all-venues`` is accepted in place of ``--league-id``."""
    args = cli._build_parser().parse_args(["--year", "2025", "--all-venues"])

    assert args.year == 2025
    assert args.league_id is None
    assert args.all_venues is True


def test_league_id_and_all_venues_are_mutually_exclusive() -> None:
    """Passing both ``--league-id`` and ``--all-venues`` is rejected."""
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(
            ["--year", "2025", "--league-id", "13", "--all-venues"]
        )


def test_missing_target_argument_is_rejected() -> None:
    """Neither ``--league-id`` nor ``--all-venues`` fails argument validation."""
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["--year", "2025"])


def test_missing_year_is_rejected() -> None:
    """Omitting the required ``--year`` argument fails validation."""
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["--league-id", "13"])


def test_main_normalizes_single_league_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` normalizes a single ``--league-id`` before running the reconcile."""
    captured: dict[str, object] = {}

    async def fake_run(*, year: int, league_ids: list[str]) -> int:
        captured["year"] = year
        captured["league_ids"] = league_ids
        return 0

    monkeypatch.setattr(cli, "_run", fake_run)

    exit_code = cli.main(["--year", "2025", "--league-id", "13"])

    assert exit_code == 0
    assert captured == {"year": 2025, "league_ids": ["13"]}


def test_main_expands_all_venues(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--all-venues`` expands to every supported LeagueID, sorted."""
    captured: dict[str, object] = {}

    async def fake_run(*, year: int, league_ids: list[str]) -> int:
        captured["year"] = year
        captured["league_ids"] = league_ids
        return 0

    monkeypatch.setattr(cli, "_run", fake_run)

    exit_code = cli.main(["--year", "2025", "--all-venues"])

    assert exit_code == 0
    assert captured["year"] == 2025
    assert captured["league_ids"] == sorted(cli.SUPPORTED_SUMMER_LEAGUES)


def test_main_rejects_unsupported_league_id() -> None:
    """An unsupported LeagueID fails validation via ``normalize_league_id``."""
    with pytest.raises(SystemExit):
        cli.main(["--year", "2025", "--league-id", "999"])
