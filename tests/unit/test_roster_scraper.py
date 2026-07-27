"""Unit tests for the NBA.com Summer League roster scraper.

Covers: __NEXT_DATA__ roster parsing, team-link enumeration, empty-roster
handling, idempotent snapshot writing, and per-team failure isolation.

All tests use captured HTML fixtures under
``tests/fixtures/summer_league/roster/``. No network calls are made.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.summer_league.roster_fetch import RosterFetcher
from app.services.summer_league.roster_parse import (
    RosterEntry,
    parse_roster,
    parse_team_links,
)
from scripts import fetch_summer_league_rosters

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_FIXTURE_DIR = (
    Path(__file__).parent.parent / "fixtures" / "summer_league" / "roster"
)


def _load_fixture(name: str) -> str:
    """Return the text content of a roster HTML fixture file."""
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_roster — populated fixture
# ---------------------------------------------------------------------------


def test_parse_next_data_roster() -> None:
    """Populated roster fixture parses to typed RosterEntry records with PERSON_ID.

    Verifies that PLAYER_ID maps to nba_stats_person_id and that all bio fields
    (position, height, weight, birth_date, school, how_acquired) are extracted.
    """
    html = _load_fixture("team_lakers_populated.html")
    entries = parse_roster(html)

    assert len(entries) == 2

    bronny = next(e for e in entries if e.raw_player_name == "Bronny James")
    assert bronny.nba_stats_person_id == "1641782"
    assert bronny.team_id == "1610612747"
    assert bronny.jersey == "9"
    assert bronny.position == "G"
    assert bronny.height == "6-2"
    assert bronny.weight == "190"
    assert bronny.birth_date == "2004-10-05T00:00:00"
    assert bronny.school == "USC"
    assert bronny.how_acquired == "Draft"
    assert bronny.league_id == "15"

    caruso = next(e for e in entries if e.raw_player_name == "Alex Caruso")
    assert caruso.nba_stats_person_id == "1629029"
    assert caruso.school == "Texas A&M"
    assert caruso.how_acquired == "Free Agent"


def test_parse_roster_returns_list_of_roster_entry_type() -> None:
    """parse_roster always returns a list of RosterEntry instances."""
    html = _load_fixture("team_lakers_populated.html")
    entries = parse_roster(html)
    assert all(isinstance(e, RosterEntry) for e in entries)


# ---------------------------------------------------------------------------
# parse_team_links — landing page fixture
# ---------------------------------------------------------------------------


def test_enumerate_team_links() -> None:
    """Landing page fixture produces all team (team_id, slug) pairs.

    Expects exactly 3 unique teams from the Las Vegas landing fixture,
    with the duplicate link deduplicated.
    """
    html = _load_fixture("landing_las_vegas.html")
    links = parse_team_links(html)

    # Duplicates must be removed; the fixture has one dup for Lakers
    assert len(links) == 3

    team_ids = [t for t, _ in links]
    assert "1610612747" in team_ids  # Lakers
    assert "1610612738" in team_ids  # Celtics
    assert "1610612744" in team_ids  # Warriors


def test_enumerate_team_links_returns_tuples() -> None:
    """parse_team_links returns list of (str, str) tuples."""
    html = _load_fixture("landing_las_vegas.html")
    links = parse_team_links(html)
    for team_id, slug in links:
        assert isinstance(team_id, str)
        assert isinstance(slug, str)


def test_enumerate_team_links_empty_page() -> None:
    """A page with no team hrefs returns an empty list without error."""
    links = parse_team_links("<html><body><p>No teams yet.</p></body></html>")
    assert links == []


# ---------------------------------------------------------------------------
# Empty roster — no crash
# ---------------------------------------------------------------------------


def test_empty_roster_no_crash() -> None:
    """roster: [] parses to zero players without raising an exception.

    This covers the live state today (2026-06-28): pages are up but rosters
    have not been announced yet.
    """
    html = _load_fixture("team_celtics_empty.html")
    entries = parse_roster(html)
    assert entries == []


def test_empty_roster_in_inline_html() -> None:
    """Inline __NEXT_DATA__ with empty roster array returns empty list."""
    html = (
        '<html><body>'
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"roster":[]}}}'
        '</script></body></html>'
    )
    entries = parse_roster(html)
    assert entries == []


def test_missing_roster_key_returns_empty() -> None:
    """pageProps without a roster key returns empty list (no crash)."""
    html = (
        '<html><body>'
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{}}}'
        '</script></body></html>'
    )
    entries = parse_roster(html)
    assert entries == []


# ---------------------------------------------------------------------------
# Snapshot idempotency
# ---------------------------------------------------------------------------


def test_snapshot_idempotent_write(tmp_path: Path) -> None:
    """Writing a roster snapshot twice produces the same file with no corruption.

    A second write without --force is a no-op (reuses the first snapshot).
    A second write with --force produces an identical deterministic file.
    """
    from app.services.summer_league.raw_store import SummerLeagueRawStore

    store = SummerLeagueRawStore(tmp_path)
    payload = {"year": 2026, "league_id": "15", "teams": []}

    path = store.season_file(year=2026, league_id="15", name="rosters")

    r1 = store.write_json(path, payload)
    assert r1.written is True

    # Second write without force → skipped
    r2 = store.write_json(path, payload)
    assert r2.written is False
    assert r2.skipped is True

    # Content is unchanged
    assert json.loads(path.read_text()) == {"league_id": "15", "teams": [], "year": 2026}

    # Force write produces identical content
    r3 = store.write_json(path, payload, force=True)
    assert r3.written is True
    assert json.loads(path.read_text()) == {"league_id": "15", "teams": [], "year": 2026}


def test_snapshot_write_is_stable_json(tmp_path: Path) -> None:
    """Snapshot JSON is sorted and terminated with a newline (stable diffs)."""
    from app.services.summer_league.raw_store import SummerLeagueRawStore

    store = SummerLeagueRawStore(tmp_path)
    path = store.season_file(year=2026, league_id="15", name="rosters")
    store.write_json(path, {"z": 1, "a": 2})

    text = path.read_text()
    assert text.endswith("\n")
    parsed = json.loads(text)
    keys = list(parsed.keys())
    assert keys == sorted(keys), "JSON keys must be sorted for stable diffs"


# ---------------------------------------------------------------------------
# Per-team failure isolation (CLI-level)
# ---------------------------------------------------------------------------


def test_partial_failure_continues(tmp_path: Path) -> None:
    """A per-team fetch failure is captured and does not abort the whole run.

    The test simulates a landing page with three teams where one team's page
    raises an error. Expects: remaining two teams are still fetched; the failed
    team has a non-None ``error``; the run still succeeds (returns a result).
    """
    landing_html = _load_fixture("landing_las_vegas.html")
    team_html_populated = _load_fixture("team_lakers_populated.html")
    team_html_empty = _load_fixture("team_celtics_empty.html")

    # Lakers (1610612747) and Warriors (1610612744) succeed;
    # Celtics (1610612738) fails.
    def fake_fetch(url: str) -> str:
        if "/team/1610612738/" in url:  # Celtics → fail
            raise RuntimeError("simulated network error")
        if "/team/1610612747/" in url:  # Lakers → populated
            return team_html_populated
        if "/team/1610612744/" in url:  # Warriors → empty
            return team_html_empty
        # Landing page
        return landing_html

    fetcher = RosterFetcher(
        timeout=5.0,
        delay_seconds=0.0,
        sleep=lambda _: None,
        fetch_fn=fake_fetch,
    )

    result = fetcher.fetch_run(
        year=2026,
        league_id="15",
        out_dir=tmp_path,
        force=False,
        dry_run=True,  # don't write; we're testing the run, not the snapshot
        verbose=False,
    )

    # Run did not crash; three teams enumerated
    assert result.team_count == 3

    # Celtics team has an error recorded
    celtics = next(t for t in result.team_results if t.team_id == "1610612738")
    assert celtics.error is not None
    assert "simulated network error" in celtics.error

    # Lakers team succeeded with 2 players
    lakers = next(t for t in result.team_results if t.team_id == "1610612747")
    assert lakers.player_count == 2
    assert lakers.error is None

    # Warriors team succeeded with 0 players (empty roster, no error)
    warriors = next(t for t in result.team_results if t.team_id == "1610612744")
    assert warriors.player_count == 0
    assert warriors.error is None


def test_player_count_dedupes_person_appearing_on_two_team_subpages(
    tmp_path: Path,
) -> None:
    """A person listed under two team subpages is only counted once.

    Regression for a fetcher bug where the reported ``players=N`` summary
    summed each team's raw roster length (e.g. 67), double-counting a
    person who legitimately appears on more than one team's roster page
    (mid-event trade), while the deduplicated set actually written to the
    snapshot was smaller (e.g. 53). ``RosterRunResult.player_count`` must
    equal the number of unique ``nba_stats_person_id`` values present in
    the written snapshot, not the sum of per-team roster lengths.
    """
    landing_html = _load_fixture("landing_las_vegas.html")
    lakers_html = _load_fixture("team_lakers_populated.html")

    # Celtics roster re-lists Bronny James (id 1641782, already on Lakers)
    # plus one new player — simulating a mid-event trade/cross-listing.
    celtics_html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"roster":['
        '{"PLAYER_ID":"1641782","PLAYER":"Bronny James","NUM":"9",'
        '"POSITION":"G","HEIGHT":"6-2","WEIGHT":"190",'
        '"BIRTH_DATE":"2004-10-05T00:00:00","SCHOOL":"USC",'
        '"HOW_ACQUIRED":"Trade","LeagueID":"15","TeamID":"1610612738"},'
        '{"PLAYER_ID":"1630000","PLAYER":"New Guy","NUM":"5",'
        '"POSITION":"F","HEIGHT":"6-8","WEIGHT":"220",'
        '"BIRTH_DATE":"2003-01-01T00:00:00","SCHOOL":"Duke",'
        '"HOW_ACQUIRED":"Free Agent","LeagueID":"15","TeamID":"1610612738"}'
        ']}},"page":"x","query":{}}</script></body></html>'
    )
    warriors_html = _load_fixture("team_celtics_empty.html")  # empty roster

    def fake_fetch(url: str) -> str:
        if "/team/1610612747/" in url:  # Lakers
            return lakers_html
        if "/team/1610612738/" in url:  # Celtics
            return celtics_html
        if "/team/1610612744/" in url:  # Warriors
            return warriors_html
        return landing_html

    fetcher = RosterFetcher(
        delay_seconds=0.0,
        sleep=lambda _: None,
        fetch_fn=fake_fetch,
    )

    result = fetcher.fetch_run(
        year=2026,
        league_id="15",
        out_dir=tmp_path,
        dry_run=False,
        verbose=False,
    )

    # Raw per-team lengths sum to 4 (2 Lakers + 2 Celtics + 0 Warriors), but
    # Bronny James is the same person on both Lakers and Celtics rosters.
    raw_total = sum(t.player_count for t in result.team_results)
    assert raw_total == 4

    # The reported count dedupes to 3 unique people.
    assert result.player_count == 3

    # The written snapshot's "player_count" field must match, and must equal
    # the number of unique nba_stats_person_id values across all team rosters
    # actually persisted.
    assert result.snapshot_path is not None
    snapshot = json.loads(Path(result.snapshot_path).read_text())
    assert snapshot["player_count"] == 3

    unique_ids_in_snapshot = {
        player["nba_stats_person_id"]
        for team in snapshot["teams"]
        for player in team["players"]
    }
    assert len(unique_ids_in_snapshot) == 3
    assert snapshot["player_count"] == len(unique_ids_in_snapshot)


def test_partial_failure_error_count(tmp_path: Path) -> None:
    """error_count reflects only failed teams, not empty rosters."""
    landing_html = _load_fixture("landing_las_vegas.html")

    def fake_fetch(url: str) -> str:
        if "/team/1610612738/" in url:  # Celtics → fail
            raise RuntimeError("boom")
        return landing_html if "/team/" not in url else (
            "<html><script id=\"__NEXT_DATA__\">"
            "{\"props\":{\"pageProps\":{\"roster\":[]}}}"
            "</script></html>"
        )

    fetcher = RosterFetcher(
        delay_seconds=0.0,
        sleep=lambda _: None,
        fetch_fn=fake_fetch,
    )
    result = fetcher.fetch_run(
        year=2026,
        league_id="15",
        out_dir=tmp_path,
        dry_run=True,
    )

    assert result.error_count == 1
    assert result.player_count == 0  # all others empty


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


def test_expand_league_ids_supports_comma_and_repeat() -> None:
    """League IDs can be comma-separated and repeated; duplicates are removed."""
    result = fetch_summer_league_rosters.expand_league_ids(["15,13", "15", "16"])
    assert result == ["15", "13", "16"]


def test_expand_league_ids_rejects_invalid() -> None:
    """Unsupported LeagueIDs raise ValueError."""
    with pytest.raises(ValueError, match="Unsupported"):
        fetch_summer_league_rosters.expand_league_ids(["99"])


def test_main_exits_non_zero_when_all_runs_fail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() exits 1 when the landing page fetch fails entirely for all venues."""

    def bad_fetch(url: str) -> str:
        raise RuntimeError("network down")

    monkeypatch.setattr(
        fetch_summer_league_rosters,
        "build_fetcher",
        lambda **kwargs: RosterFetcher(
            delay_seconds=0.0,
            sleep=lambda _: None,
            fetch_fn=bad_fetch,
        ),
    )

    exit_code = fetch_summer_league_rosters.main(
        ["--year", "2026", "--league-id", "15", "--out-dir", str(tmp_path)]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "failed" in captured.err or "failed" in captured.out
