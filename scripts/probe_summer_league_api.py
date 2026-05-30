"""Exploratory probe of the NBA Stats API for Summer League coverage.

This is a THROWAWAY exploration script, not production ingestion. Its job is to
replace the assumptions in ``docs/summer_league_stats_plan.md`` with reality:

1. Which ``LeagueID`` values map to which Summer League venues, per year.
2. Which ``Season`` string format the API accepts ("2015" vs "2015-16").
3. Box-score availability per era (via ``leaguegamelog``).
4. Tier-2 (shot detail) and Tier-3 (play-by-play) availability for one game/era.
5. Entity-resolution anchors: presence of ``PERSON_ID`` / ``GAME_ID`` fields.

It writes raw JSON snapshots to ``scripts/data/sl_probe/`` (so re-parsing is free)
and prints a coverage summary. No DB writes, no app imports.

Run::

    conda run -n draftguru python scripts/probe_summer_league_api.py --verbose

Header/client pattern is lifted from ``scripts/nba_draft_scraper.py`` which already
talks to ``stats.nba.com`` successfully from this repo.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# stats.nba.com sits behind Akamai bot-management that tarpits generic HTTP
# clients (httpx/curl) by TLS/JA3 fingerprint. curl_cffi with browser
# impersonation reproduces Chrome's fingerprint and gets through. See
# docs/summer_league_api_probe_findings.md for the diagnosis.
from curl_cffi import requests as cffi

# ---------------------------------------------------------------------------
# Client config (proven pattern from scripts/nba_draft_scraper.py)
# ---------------------------------------------------------------------------
NBA_API_ROOT = "https://stats.nba.com/stats"

# Only the nba-specific headers; impersonate="chrome" supplies the browser
# headers (User-Agent, sec-ch-ua, ...) consistent with the spoofed fingerprint.
NBA_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "Accept-Language": "en-US,en;q=0.9",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}

OUT_DIR = Path(__file__).resolve().parent / "data" / "sl_probe"

# LeagueIDs to test. 00 = NBA (control, should always work). 13/14/15/16 are the
# candidate Summer League ids the tech spec is unsure about.
LEAGUE_IDS = ["00", "13", "14", "15", "16"]

# Years spanning the proposed backfill range. 2020 Vegas SL was cancelled (COVID),
# so 2021 stands in for the early-2020s data point.
SEASONS = ["2004", "2010", "2015", "2019", "2021", "2024", "2025"]

# Politeness delay between requests (seconds).
DELAY = 0.7


# ---------------------------------------------------------------------------
# Generic fetch + snapshot
# ---------------------------------------------------------------------------
@dataclass
class FetchResult:
    endpoint: str
    params: dict[str, str]
    status: Optional[int] = None
    ok: bool = False
    error: Optional[str] = None
    result_sets: dict[str, int] = field(default_factory=dict)  # name -> row count
    headers_by_set: dict[str, list[str]] = field(default_factory=dict)
    snapshot_path: Optional[str] = None
    payload: Optional[dict] = None  # kept in-memory only when needed downstream


def _slug(params: dict[str, str]) -> str:
    parts = []
    for k in ("LeagueID", "Season", "PlayerOrTeam", "GameID"):
        if params.get(k):
            parts.append(f"{k}-{params[k]}")
    return "_".join(parts) if parts else "noparams"


def fetch(
    client: "cffi.Session",
    endpoint: str,
    params: dict[str, str],
    *,
    keep_payload: bool = False,
    verbose: bool = False,
) -> FetchResult:
    """Hit one stats.nba.com endpoint, snapshot the raw response, summarize it."""
    url = f"{NBA_API_ROOT}/{endpoint}"
    res = FetchResult(endpoint=endpoint, params=dict(params))
    try:
        r = client.get(url, params=params)
        res.status = r.status_code
        if r.status_code >= 400:
            res.error = f"HTTP {r.status_code}"
            time.sleep(DELAY)
            return res
        payload = r.json()
    except json.JSONDecodeError:
        res.error = "non_json_response"
    except Exception as exc:  # curl_cffi raises its own RequestsError/timeout types
        msg = type(exc).__name__
        res.error = "timeout" if "timeout" in str(exc).lower() else f"err: {msg}"
    else:
        res.ok = True
        sets = payload.get("resultSets")
        if isinstance(sets, dict):  # some endpoints (boxscore) nest a single dict
            sets = [sets]
        if sets is None:
            single = payload.get("resultSet")
            sets = [single] if single else []
        for s in sets or []:
            name = s.get("name", "?")
            rows = s.get("rowSet") or []
            res.result_sets[name] = len(rows)
            res.headers_by_set[name] = s.get("headers", [])
        if keep_payload:
            res.payload = payload

        # Snapshot raw JSON to disk.
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{endpoint}__{_slug(params)}.json"
        path = OUT_DIR / fname
        path.write_text(json.dumps(payload, indent=2))
        res.snapshot_path = str(path.relative_to(OUT_DIR.parent.parent))

    if verbose:
        status = res.error or f"OK {res.result_sets}"
        print(f"  [{endpoint}] {_slug(params)} -> {status}")
    time.sleep(DELAY)
    return res


# ---------------------------------------------------------------------------
# Endpoint param builders (these APIs require ALL params present, even if blank)
# ---------------------------------------------------------------------------
def p_leaguegamelog(league_id: str, season: str, player_or_team: str) -> dict[str, str]:
    return {
        "Counter": "1000",
        "Direction": "DESC",
        "LeagueID": league_id,
        "PlayerOrTeam": player_or_team,  # "P" or "T"
        "Season": season,
        "SeasonType": "Regular Season",
        "Sorter": "DATE",
    }


def p_commonallplayers(league_id: str, season: str) -> dict[str, str]:
    return {
        "LeagueID": league_id,
        "Season": season,
        "IsOnlyCurrentSeason": "0",
    }


def p_boxscore(game_id: str) -> dict[str, str]:
    return {
        "GameID": game_id,
        "StartPeriod": "0",
        "EndPeriod": "10",
        "StartRange": "0",
        "EndRange": "28800",
        "RangeType": "0",
    }


def p_playbyplay(game_id: str) -> dict[str, str]:
    return {"GameID": game_id, "StartPeriod": "0", "EndPeriod": "10"}


def p_shotchart(league_id: str, season: str, game_id: str) -> dict[str, str]:
    return {
        "LeagueID": league_id,
        "Season": season,
        "SeasonType": "Regular Season",
        "TeamID": "0",
        "PlayerID": "0",
        "GameID": game_id,
        "Outcome": "",
        "Location": "",
        "Month": "0",
        "SeasonSegment": "",
        "DateFrom": "",
        "DateTo": "",
        "OpponentTeamID": "0",
        "VsConference": "",
        "VsDivision": "",
        "Position": "",
        "RookieYear": "",
        "GameSegment": "",
        "Period": "0",
        "LastNGames": "0",
        "ContextMeasure": "FGA",
        "PlayerPosition": "",
    }


# ---------------------------------------------------------------------------
# Probe phases
# ---------------------------------------------------------------------------
def gamelog_rows(res: FetchResult) -> tuple[list[str], list[list[Any]]]:
    """Return (headers, rows) for the first result set of a payload kept in memory."""
    if not res.payload:
        return [], []
    sets = res.payload.get("resultSets") or []
    if not sets:
        return [], []
    s = sets[0]
    return s.get("headers", []), s.get("rowSet", [])


def discover_coverage(client: cffi.Session, verbose: bool) -> list[FetchResult]:
    """Phase 1: map LeagueID x Season coverage via team-level leaguegamelog."""
    print("\n=== Phase 1: coverage matrix (leaguegamelog, PlayerOrTeam=T) ===")
    results: list[FetchResult] = []
    for league_id in LEAGUE_IDS:
        for season in SEASONS:
            res = fetch(
                client,
                "leaguegamelog",
                p_leaguegamelog(league_id, season, "T"),
                keep_payload=True,
                verbose=verbose,
            )
            results.append(res)
    return results


def pick_probe_targets(
    coverage: list[FetchResult],
) -> dict[str, FetchResult]:
    """Pick one populated (league_id, season) per era to drill into.

    Returns map of era-label -> the coverage FetchResult, choosing the first
    non-empty, non-NBA league for each target year we care about.
    """
    targets: dict[str, FetchResult] = {}
    want_years = ["2024", "2025", "2019", "2015", "2010"]
    for year in want_years:
        for res in coverage:
            if res.params.get("Season") != year:
                continue
            if res.params.get("LeagueID") == "00":
                continue  # skip NBA control for drill-down
            total = sum(res.result_sets.values())
            if res.ok and total > 0:
                targets[year] = res
                break
    return targets


def first_game_id(res: FetchResult) -> Optional[str]:
    headers, rows = gamelog_rows(res)
    if not rows:
        return None
    try:
        idx = headers.index("GAME_ID")
    except ValueError:
        return None
    return str(rows[0][idx])


def drill_game(
    client: cffi.Session,
    league_id: str,
    season: str,
    game_id: str,
    verbose: bool,
) -> dict[str, FetchResult]:
    """Phase 2: per-game detail across tiers for a single GAME_ID."""
    out: dict[str, FetchResult] = {}
    out["boxscore_traditional"] = fetch(
        client, "boxscoretraditionalv2", p_boxscore(game_id), verbose=verbose
    )
    out["boxscore_advanced"] = fetch(
        client, "boxscoreadvancedv2", p_boxscore(game_id), verbose=verbose
    )
    out["boxscore_scoring"] = fetch(
        client, "boxscorescoringv2", p_boxscore(game_id), verbose=verbose
    )
    out["playbyplay"] = fetch(
        client, "playbyplayv2", p_playbyplay(game_id), verbose=verbose
    )
    out["shotchart"] = fetch(
        client,
        "shotchartdetail",
        p_shotchart(league_id, season, game_id),
        verbose=verbose,
    )
    return out


def probe_rosters(
    client: cffi.Session, targets: dict[str, FetchResult], verbose: bool
) -> dict[str, FetchResult]:
    """Phase 3: commonallplayers for each drilled (league, season)."""
    print("\n=== Phase 3: rosters (commonallplayers) ===")
    out: dict[str, FetchResult] = {}
    seen: set[tuple[str, str]] = set()
    for year, cov in targets.items():
        lid = cov.params["LeagueID"]
        season = cov.params["Season"]
        if (lid, season) in seen:
            continue
        seen.add((lid, season))
        out[year] = fetch(
            client,
            "commonallplayers",
            p_commonallplayers(lid, season),
            keep_payload=True,
            verbose=verbose,
        )
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_coverage_matrix(coverage: list[FetchResult]) -> None:
    print("\n--- Coverage matrix (rows = teams in leaguegamelog) ---")
    header = "League | " + " | ".join(f"{s:>6}" for s in SEASONS)
    print(header)
    print("-" * len(header))
    for league_id in LEAGUE_IDS:
        cells = []
        for season in SEASONS:
            match = next(
                (
                    r
                    for r in coverage
                    if r.params.get("LeagueID") == league_id
                    and r.params.get("Season") == season
                ),
                None,
            )
            if match is None:
                cells.append("   ?  ")
            elif match.error:
                cells.append(f"{match.error[:6]:>6}")
            else:
                total = sum(match.result_sets.values())
                cells.append(f"{total:>6}")
        print(f"  {league_id}   | " + " | ".join(cells))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    print(f"Snapshots -> {OUT_DIR}")
    with cffi.Session(
        headers=NBA_API_HEADERS, impersonate="chrome", timeout=args.timeout
    ) as client:
        coverage = discover_coverage(client, args.verbose)
        print_coverage_matrix(coverage)

        targets = pick_probe_targets(coverage)
        print("\nDrill-down targets (year -> league/season): ")
        for year, cov in targets.items():
            print(
                f"  {year}: LeagueID={cov.params['LeagueID']} Season={cov.params['Season']}"
            )

        print("\n=== Phase 2: per-game detail drill-down ===")
        game_summaries: dict[str, dict[str, FetchResult]] = {}
        for year, cov in targets.items():
            gid = first_game_id(cov)
            if not gid:
                print(f"  {year}: no GAME_ID found, skipping drill-down")
                continue
            lid = cov.params["LeagueID"]
            season = cov.params["Season"]
            print(f"  {year}: drilling GAME_ID={gid} (LeagueID={lid})")
            game_summaries[year] = drill_game(client, lid, season, gid, args.verbose)

        rosters = probe_rosters(client, targets, args.verbose)

    # Final compact summary
    print("\n========== SUMMARY ==========")
    print_coverage_matrix(coverage)
    print("\nPer-game tier availability (rows returned per endpoint):")
    for year, summ in game_summaries.items():
        print(f"\n  {year}:")
        for ep, res in summ.items():
            status = res.error or str(res.result_sets)
            print(f"    {ep:24} {status}")
    print("\nRoster availability (commonallplayers rows):")
    for year, res in rosters.items():
        status = res.error or str(res.result_sets)
        print(f"  {year}: {status}")
    print(f"\nRaw snapshots written under {OUT_DIR}")


if __name__ == "__main__":
    main()
