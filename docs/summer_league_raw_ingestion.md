# Summer League Raw Ingestion

This document covers the first production data-acquisition path for DraftGuru's
Summer League stats project. It fetches raw NBA.com Stats JSON snapshots and
writes local files only. It does not write to the database, create
`summer_league_*` tables, resolve players, or update public pages.

## References

- Planning spec: `docs/plans/summer-league-raw-ingestion-workflow.md`
- Feature plan: `docs/summer_league_stats_plan.md`
- API probe findings: `docs/summer_league_api_probe_findings.md`
- Probe script retained for exploration: `scripts/probe_summer_league_api.py`

## NBA.com Access Caveat

`stats.nba.com` tarpits plain HTTP clients by TLS/browser fingerprint. The raw
fetcher uses `curl_cffi==0.7.4` through `NBAStatsClient`, with
`impersonate="chrome"` and the NBA Stats headers validated during the probe.

If the client import fails, reinstall the project dependencies in the Conda env:

```bash
conda run -n draftguru python -m pip install -e ".[dev]"
```

## Supported League IDs

| LeagueID | Venue |
|---|---|
| `15` | Las Vegas Summer League |
| `13` | California Classic |
| `16` | Salt Lake City Summer League |
| `14` | Orlando Pro Summer League |

Summer League seasons use a bare four-digit year, such as `2024`.

## Basic Usage

Fetch one full year/venue:

```bash
conda run -n draftguru python scripts/fetch_summer_league_raw.py --year 2024 --league-id 15
```

Fetch multiple venues:

```bash
conda run -n draftguru python scripts/fetch_summer_league_raw.py --year 2024 --league-id 15,13,16
```

Run a one-game smoke test:

```bash
conda run -n draftguru python scripts/fetch_summer_league_raw.py --year 2024 --league-id 15 --limit-games 1
```

Plan a run without per-game detail fetches or raw payload writes:

```bash
conda run -n draftguru python scripts/fetch_summer_league_raw.py --year 2024 --league-id 15 --dry-run --limit-games 1
```

Useful options:

- `--out-dir PATH` changes the local raw root.
- `--timeout SECONDS` controls NBA Stats request timeout.
- `--delay SECONDS` controls the politeness delay between requests.
- `--retries N` retries transient transport failures, HTTP 429, and 5xx
  responses. Default: `3`.
- `--retry-delay SECONDS` controls the base retry backoff delay. Default: `2`.
- `--limit-games N` fetches per-game detail for only the first `N` discovered games.
- `--force` overwrites existing snapshots instead of reusing them.
- `--dry-run` discovers games and writes a manifest, but skips raw payload writes
  and per-game detail requests.
- `--verbose` prints per-league, per-game, and per-file progress. Use this for
  long backfills.
- `--skip-endpoint ENDPOINT` skips a per-game detail endpoint. Repeat or
  comma-separate values. This is useful for historical eras where an endpoint is
  known to be empty or consistently failing, such as pre-2019 `playbyplayv2`.

By default, the fetcher is resume-friendly: if a raw snapshot already exists and
`--force` is not set, the file is reused and the request is skipped. This lets
long scrapes resume after a severed connection by rerunning the same command.

## Output Layout

Default output root:

```text
data/raw/nba_stats/summer_league/{year}/{league_id}/
```

Example:

```text
data/raw/nba_stats/summer_league/2024/15/
  manifest.json
  leaguegamelog_team.json
  leaguegamelog_player.json
  games/
    1522400076/
      boxscoretraditionalv2.json
      boxscoreadvancedv2.json
      boxscorescoringv2.json
      playbyplayv2.json
      shotchartdetail.json
```

`data/raw/` is ignored by Git.

## Manifest

Each `(year, league_id)` run writes `manifest.json`. The manifest records:

- `year`, `league_id`, and inferred `venue`
- `started_at` / `finished_at`
- `team_gamelog_rows` and `player_gamelog_rows`
- unique `game_ids` discovered from the team gamelog
- `game_count`
- `files_written`
- `files_skipped`
- recoverable endpoint `errors`

Endpoint errors are per-file/per-game. A failed play-by-play request for one
game does not discard successful box score snapshots from that game or other
games.

## Expected First Target

Use 2024 Las Vegas (`LeagueID=15`) as the first source of truth for downstream
normalization work:

```bash
conda run -n draftguru python scripts/fetch_summer_league_raw.py --year 2024 --league-id 15 --limit-games 1
```

After the smoke test, inspect:

```text
data/raw/nba_stats/summer_league/2024/15/manifest.json
```

For full backfill, start with modern years and main Vegas coverage:

```bash
conda run -n draftguru python scripts/fetch_summer_league_raw.py --year 2024 --league-id 15,13,16
conda run -n draftguru python scripts/fetch_summer_league_raw.py --year 2025 --league-id 15,13,16
conda run -n draftguru python scripts/fetch_summer_league_raw.py --year 2019 --league-id 15,13,16
```

Older Orlando and early Vegas seasons should be treated as partial until their
raw manifests prove endpoint coverage.
