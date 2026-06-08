# Summer League Raw Ingestion Workflow

## Goal

Build the first production-ready data acquisition slice for DraftGuru's Summer
League stats feature: a reusable NBA.com Stats API client, deterministic raw
JSON snapshot storage, and a CLI that can fetch one or more Summer League
season/venue datasets without writing to the database.

This project intentionally stops at raw ingestion. Normalized
`summer_league_*` tables, player entity resolution, public routes, and UI pages
come after the raw source data is reliable and inspectable.

## Background

Primary references:

- `docs/summer_league_stats_plan.md` — full Summer League feature plan.
- `docs/summer_league_api_probe_findings.md` — validated NBA.com Stats access
  pattern, LeagueID mapping, season format, and availability matrix.
- `scripts/probe_summer_league_api.py` — exploratory script that proved the
  API path but is not production ingestion code.

Important findings from the probe:

- `stats.nba.com` requires TLS/browser impersonation; plain `httpx` and `curl`
  tarpit. Use `curl_cffi==0.7.4` with `impersonate="chrome"`.
- Summer League `LeagueID` values:
  - `15` = Las Vegas Summer League
  - `13` = California Classic
  - `16` = Salt Lake City Summer League
  - `14` = Orlando Pro Summer League
- `Season` is a bare four-digit year string, e.g. `2024`.
- Player universe must be harvested from `leaguegamelog` / box scores, not
  `commonallplayers`.
- Stable external anchors are `PLAYER_ID`/`PERSON_ID` and `GAME_ID`.

## Scope

### In Scope

- Add the pinned `curl_cffi==0.7.4` dependency.
- Create `app/services/summer_league/` with reusable, tested raw-fetch helpers.
- Implement endpoint parameter builders for:
  - `leaguegamelog`
  - `boxscoretraditionalv2`
  - `boxscoreadvancedv2`
  - `boxscorescoringv2`
  - `playbyplayv2`
  - `shotchartdetail`
- Implement deterministic local raw snapshot storage.
- Implement a CLI that can fetch a full year/league combination, starting with
  `--year 2024 --league-id 15`.
- Write a manifest per run with row counts, discovered game IDs, files written,
  errors, and timestamps.
- Add docs describing how to run the fetcher and how to interpret output.

### Out of Scope

- No database schema or Alembic migrations.
- No normalized Summer League tables.
- No player matching or `player_external_ids` writes.
- No S3 upload in this slice; local raw files are enough. The raw-store API
  should not preclude S3 later.
- No public routes, templates, or visual work.
- No live polling or scheduled jobs.

## Target Raw Layout

Default local output path:

```text
data/raw/nba_stats/summer_league/{year}/{league_id}/
  manifest.json
  leaguegamelog_team.json
  leaguegamelog_player.json
  games/
    {game_id}/
      boxscoretraditionalv2.json
      boxscoreadvancedv2.json
      boxscorescoringv2.json
      playbyplayv2.json
      shotchartdetail.json
```

The path should be overridable for tests and one-off runs.

## CLI Contract

Initial command:

```bash
conda run -n draftguru python scripts/fetch_summer_league_raw.py --year 2024 --league-id 15
```

Useful options:

- `--year YEAR` — required, four-digit year.
- `--league-id ID` — repeatable or comma-separated; supports `13`, `14`, `15`,
  `16`.
- `--out-dir PATH` — optional local raw root override.
- `--timeout SECONDS` — optional NBA Stats request timeout.
- `--delay SECONDS` — optional politeness delay between NBA Stats requests.
- `--limit-games N` — optional dev/testing limiter.
- `--force` — refetch even when a snapshot file already exists.
- `--dry-run` — discover game IDs and print planned writes without fetching
  per-game detail.

## Manifest Contract

Each `(year, league_id)` run writes `manifest.json`:

```json
{
  "year": 2024,
  "league_id": "15",
  "venue": "las_vegas",
  "started_at": "2026-06-07T12:00:00Z",
  "finished_at": "2026-06-07T12:04:00Z",
  "team_gamelog_rows": 152,
  "player_gamelog_rows": 900,
  "game_ids": ["1522400001"],
  "game_count": 76,
  "files_written": ["leaguegamelog_team.json"],
  "files_skipped": [],
  "errors": []
}
```

Errors should be captured per endpoint/game and should not erase successful
snapshots from the same run.

## Testing Strategy

All unit tests must avoid live NBA.com network calls. Use fake sessions /
responses and temporary directories.

Expected unit coverage:

- Endpoint builders produce complete NBA Stats parameter dictionaries.
- League ID / venue mapping validates supported IDs.
- Client wrapper handles result-set shapes from NBA Stats payloads.
- Raw store writes deterministic paths and can skip existing files unless
  `force=True`.
- Manifest aggregation records counts, skipped files, and errors.
- CLI planning logic derives game IDs from team gamelog rows and respects
  `--dry-run`, `--limit-games`, and `--force`.

Manual smoke test after implementation:

```bash
conda run -n draftguru python scripts/fetch_summer_league_raw.py --year 2024 --league-id 15 --limit-games 1
```

This smoke test requires network access and the pinned `curl_cffi` dependency.

## Definition of Done

- Running the CLI for `2024 / 15` can produce a local raw snapshot tree and
  manifest for at least one limited game.
- The code path is reusable from future normalization jobs; the exploratory
  `scripts/probe_summer_league_api.py` remains a probe, not the production path.
- Unit tests cover pure logic without network.
- Required repo checks pass through Conda:
  - `conda run -n draftguru make precommit`
  - `conda run -n draftguru mypy app --ignore-missing-imports`
  - `conda run -n draftguru pytest tests/unit -q`
  - `conda run -n draftguru make coverage.diff TESTS=tests/unit`
