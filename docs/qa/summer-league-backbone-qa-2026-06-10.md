# Summer League Backbone QA Report

- Generated: `2026-06-10T03:00:08.447006+00:00`
- Raw root: `data/raw/nba_stats/summer_league`
- Status: `PASS`
- Slices: `2024/15, 2024/13, 2010/14, 2007/15`
- Total findings: `11`

## Severity Counts

| Severity | Count |
| --- | ---: |
| info | 0 |
| warning | 11 |
| error | 0 |

## Finding Codes

| Code | Count |
| --- | ---: |
| `NORMALIZATION_PLAYER_LOG_COUNT_MISMATCH` | 4 |
| `RAW_FILE_MISSING` | 1 |
| `RAW_FILE_ROW_COUNT_MISSING` | 4 |
| `RAW_RUN_ERRORS_RECORDED` | 1 |
| `RAW_RUN_INCOMPLETE` | 1 |

## Failure Summary

No blocking/error findings.

## Examples

### `RAW_FILE_ROW_COUNT_MISSING`

- Severity: `warning`
- Message: Parsed raw file is missing a row count.
- Evidence: `{"endpoint": "manifest", "game_id": null, "raw_file_id": 1, "relative_path": "2024/15/manifest.json"}`

### `NORMALIZATION_PLAYER_LOG_COUNT_MISMATCH`

- Severity: `warning`
- Message: Normalized player game-log count does not match raw player rows.
- Evidence: `{"actual": 2203, "competition_id": 1, "expected": 1581}`

### `RAW_FILE_ROW_COUNT_MISSING`

- Severity: `warning`
- Message: Parsed raw file is missing a row count.
- Evidence: `{"endpoint": "manifest", "game_id": null, "raw_file_id": 384, "relative_path": "2024/13/manifest.json"}`

### `NORMALIZATION_PLAYER_LOG_COUNT_MISMATCH`

- Severity: `warning`
- Message: Normalized player game-log count does not match raw player rows.
- Evidence: `{"actual": 336, "competition_id": 2, "expected": 270}`

### `RAW_RUN_INCOMPLETE`

- Severity: `warning`
- Message: Raw audit run is not marked complete.
- Evidence: `{"raw_run_id": 3, "status": "PARTIAL"}`


_Showing 5 of 11 findings._

## Backfill Evidence

Commands ran against disposable Postgres `draftguru_test` on localhost port `55432`
with `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test`.

| Slice | Quality | Raw files | Parse failures | Games | Teams | Team logs | Source players | Player logs | Resolved | Unresolved |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `2024/15` | `full` | 383 | 0 | 76 | 30 | 152 | 454 | 2203 | 324 | 129 |
| `2024/13` | `full` | 63 | 0 | 12 | 8 | 24 | 121 | 336 | 93 | 26 |
| `2010/14` | `partial` | 103 | 0 | 20 | 8 | 40 | 89 | 357 | 39 | 33 |
| `2007/15` | `full` | 278 | 0 | 55 | 22 | 110 | 221 | 913 | 105 | 81 |

Backfill report artifacts:

- `docs/qa/summer-league-backfill-2024-15.json`
- `docs/qa/summer-league-backfill-2024-13.json`
- `docs/qa/summer-league-backfill-2010-14.json`
- `docs/qa/summer-league-backfill-2007-15.json`

## Accepted Warnings

- `RAW_RUN_INCOMPLETE`, `RAW_RUN_ERRORS_RECORDED`, and `RAW_FILE_MISSING` are
  limited to `2010/14` game `1421000004` where NBA Stats returned HTTP 500 for
  `playbyplayv2`. The normalized backbone still loaded all 20 games, 40 team
  logs, and 357 player logs for the slice, so this is accepted as a
  non-blocking optional historical game-detail gap.
- `NORMALIZATION_PLAYER_LOG_COUNT_MISMATCH` compares season
  `leaguegamelog_player` rows with normalized per-game boxscore player rows.
  Boxscore rows are the product source for player logs and include DNP/bench
  participation differences that do not map one-to-one to the season gamelog;
  these are warning-only parity signals.
- `RAW_FILE_ROW_COUNT_MISSING` appears only on generated `manifest.json` files.
  Manifests do not contain a result-set row count by design, and every audited
  data endpoint reported row counts.

## Regression Fixes

- Imported `NbaTeam` in `app/services/sources/summer_league/normalization.py` so CLI
  backfill processes register the `nba_teams` table before creating normalized
  Summer League rows with foreign-key metadata.
- Added season-gamelog fallback team logs for historical games where
  `boxscoretraditionalv2` omits a team row. This fixed the `2007/15` Team China
  parity gap from 105 to 110 normalized team logs.
- Downgraded missing optional game-detail endpoint findings
  (`playbyplayv2`/`shotchartdetail`) to warnings when they are the only raw
  failures on a partial historical run. Core endpoint failures still produce
  error-severity QA findings.

## Final Checks

| Check | Result |
| --- | --- |
| `conda run -n draftguru --no-capture-output make precommit` | Passed |
| `conda run -n draftguru --no-capture-output mypy app --ignore-missing-imports` | Passed |
| `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test SECRET_KEY=test-secret ENV=dev conda run -n draftguru --no-capture-output python -m pytest tests/unit -q` | Passed, 694 tests before the final coverage test addition |
| `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test SECRET_KEY=test-secret ENV=dev conda run -n draftguru --no-capture-output make coverage.diff TESTS=tests/unit` | Passed, 695 tests, 96% diff coverage |
| `TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test PYTEST_ALLOW_DB=1 PYTEST_ALLOW_TEST_DB_EQUALS_DATABASE_URL=1 SECRET_KEY=test-secret ENV=dev conda run -n draftguru --no-capture-output python -m pytest tests/integration -q` | Passed, 738 tests |
| `DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test SECRET_KEY=test-secret ENV=dev conda run -n draftguru --no-capture-output python scripts/qa_summer_league_backbone.py --slice 2024/15 --slice 2024/13 --slice 2010/14 --slice 2007/15 --raw-root data/raw/nba_stats/summer_league --report-path docs/qa/summer-league-backbone-qa-2026-06-10.md` | Passed, 11 warnings, 0 errors |
