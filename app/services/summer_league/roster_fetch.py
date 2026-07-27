"""Fetch and snapshot NBA.com Summer League roster pages.

The venue landing page is fetched to enumerate teams, each per-team roster
page is fetched and parsed (see :mod:`app.services.summer_league.roster_parse`),
and one deterministic raw JSON snapshot is written per ``(year, league_id)``
run under::

    data/raw/nba_stats/summer_league/{year}/{league_id}/rosters.json

Re-running is idempotent; pass ``force=True`` to overwrite existing snapshots.
Per-team failures are captured on the result and do not abort the run.

Plain HTTP (normal User-Agent) is used for www.nba.com pages -- no
``curl_cffi`` impersonation is required (only stats.nba.com needs it).

URLs (resolved in A0 spike, 2026-06-28):

- Landing: ``https://www.nba.com/2026-summer-league-{venue_key}-roster``
- Team:    ``https://www.nba.com/summer-league/2026/{venue_path}/team/{TeamID}/{slug}``

Both the shipped roster cron (``app/cli/summer_league_roster_runner.py``) and
the operator CLI (``scripts/fetch_summer_league_rosters.py``) drive this
module; neither owns the logic.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.services.summer_league.endpoints import normalize_league_id, normalize_season
from app.services.summer_league.raw_store import SummerLeagueRawStore
from app.services.summer_league.roster_parse import (
    RosterEntry,
    parse_roster,
    parse_team_links,
)

NBA_COM_ROOT = "https://www.nba.com"
NBA_COM_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Mapping from LeagueID → (landing URL key, per-team URL path segment)
_VENUE_CONFIG: dict[str, tuple[str, str]] = {
    "15": ("vegas", "las-vegas"),
    "13": ("california", "california"),
    "16": ("slc", "salt-lake-city"),
}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _fetch_html(url: str, *, timeout: float = 30.0) -> str:
    """Fetch a URL and return the decoded HTML body.

    Args:
        url: Full URL to fetch.
        timeout: Socket timeout in seconds.

    Returns:
        Decoded HTML string.

    Raises:
        urllib.error.URLError: If the request fails (network or HTTP error).
    """
    req = urllib.request.Request(url, headers={"User-Agent": NBA_COM_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _utc_now_iso() -> str:
    """Return the current UTC time as a compact ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Run-level data structures
# ---------------------------------------------------------------------------


@dataclass
class TeamFetchResult:
    """Result of fetching and parsing one team's roster page."""

    team_id: str
    slug: str
    url: str
    players: list[dict[str, Any]] = field(default_factory=list)
    player_count: int = 0
    error: str | None = None


@dataclass
class RosterRunResult:
    """Aggregate result for one (year, league_id) roster run."""

    year: int
    league_id: str
    venue_key: str
    venue_path: str
    started_at: str = ""
    finished_at: str | None = None
    team_results: list[TeamFetchResult] = field(default_factory=list)
    snapshot_path: str | None = None
    error: str | None = None

    @property
    def team_count(self) -> int:
        """Number of teams enumerated."""
        return len(self.team_results)

    @property
    def player_count(self) -> int:
        """Count of unique players across all team rosters.

        A person can legitimately appear under more than one team subpage
        (e.g. a mid-event trade re-lists them on the new team's roster
        page), so summing each team's raw ``player_count`` over-counts.
        This tallies distinct ``nba_stats_person_id`` values instead,
        matching the unique set actually persisted downstream.
        """
        unique_person_ids = {
            player.get("nba_stats_person_id")
            for t in self.team_results
            for player in t.players
            if player.get("nba_stats_person_id")
        }
        return len(unique_person_ids)

    @property
    def error_count(self) -> int:
        """Number of team-level fetch/parse errors."""
        return sum(1 for t in self.team_results if t.error is not None)

    def to_snapshot_dict(self) -> dict[str, Any]:
        """Serialize the run to a JSON-compatible snapshot dictionary."""
        return {
            "year": self.year,
            "league_id": self.league_id,
            "venue_key": self.venue_key,
            "venue_path": self.venue_path,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "team_count": self.team_count,
            "player_count": self.player_count,
            "error_count": self.error_count,
            "teams": [
                {
                    "team_id": t.team_id,
                    "slug": t.slug,
                    "url": t.url,
                    "player_count": t.player_count,
                    "players": t.players,
                    "error": t.error,
                }
                for t in self.team_results
            ],
        }


# ---------------------------------------------------------------------------
# Fetcher (injectable for tests)
# ---------------------------------------------------------------------------


def _roster_entry_to_dict(entry: RosterEntry) -> dict[str, Any]:
    """Convert a ``RosterEntry`` dataclass to a JSON-serializable dict."""
    return asdict(entry)


class RosterFetcher:
    """Fetches and parses NBA.com Summer League roster pages."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        delay_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        fetch_fn: Callable[[str], str] | None = None,
    ) -> None:
        """Initialize the fetcher.

        Args:
            timeout: HTTP request timeout in seconds.
            delay_seconds: Polite delay between per-team requests.
            sleep: Injectable sleep function (for tests).
            fetch_fn: Injectable HTML-fetch function (for tests).
        """
        self.timeout = timeout
        self.delay_seconds = delay_seconds
        self.sleep = sleep
        self._fetch_html = fetch_fn or (lambda url: _fetch_html(url, timeout=timeout))

    def fetch_run(
        self,
        *,
        year: int,
        league_id: str,
        out_dir: Path,
        force: bool = False,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> RosterRunResult:
        """Fetch and snapshot rosters for one (year, league_id) venue.

        Args:
            year: Summer League season year (e.g. 2026).
            league_id: NBA Stats LeagueID (``"15"``, ``"13"``, or ``"16"``).
            out_dir: Raw snapshot root directory.
            force: Overwrite existing snapshot when True.
            dry_run: Parse but do not write snapshot.
            verbose: Print per-team progress.

        Returns:
            Structured run result including all team roster data and metadata.
        """
        season = normalize_season(year)
        lid = normalize_league_id(league_id)
        venue_key, venue_path = _VENUE_CONFIG[lid]

        result = RosterRunResult(
            year=int(season),
            league_id=lid,
            venue_key=venue_key,
            venue_path=venue_path,
            started_at=_utc_now_iso(),
        )

        # --- 1. Fetch landing page and enumerate teams ---
        landing_url = f"{NBA_COM_ROOT}/{season}-summer-league-{venue_key}-roster"
        if verbose:
            print(f"Fetching landing page: {landing_url}", flush=True)

        try:
            landing_html = self._fetch_html(landing_url)
        except Exception as exc:
            result.error = f"Landing page fetch failed: {type(exc).__name__}: {exc}"
            result.finished_at = _utc_now_iso()
            return result

        team_links = parse_team_links(landing_html)
        if verbose:
            print(f"  Found {len(team_links)} teams", flush=True)

        # --- 2. Fetch and parse each team page ---
        for i, (team_id, slug) in enumerate(team_links):
            team_url = f"{NBA_COM_ROOT}/summer-league/{season}/{venue_path}/team/{team_id}/{slug}"
            if verbose:
                print(f"  [{i + 1}/{len(team_links)}] {team_id}/{slug}", flush=True)

            team_result = TeamFetchResult(team_id=team_id, slug=slug, url=team_url)
            try:
                team_html = self._fetch_html(team_url)
                entries = parse_roster(team_html)
                team_result.players = [_roster_entry_to_dict(e) for e in entries]
                team_result.player_count = len(entries)
                if verbose:
                    print(f"    → {len(entries)} rostered", flush=True)
            except Exception as exc:
                team_result.error = f"{type(exc).__name__}: {exc}"
                if verbose:
                    print(f"    → ERROR: {team_result.error}", flush=True)

            result.team_results.append(team_result)

            # Polite delay between team requests (skip after last)
            if i < len(team_links) - 1 and self.delay_seconds > 0:
                self.sleep(self.delay_seconds)

        result.finished_at = _utc_now_iso()

        # --- 3. Write snapshot ---
        if not dry_run:
            store = SummerLeagueRawStore(out_dir)
            snapshot_path = store.season_file(
                year=season, league_id=lid, name="rosters"
            )
            snapshot_dict = result.to_snapshot_dict()
            store.write_json(snapshot_path, snapshot_dict, force=force)
            result.snapshot_path = str(snapshot_path)

        return result
