# Summer League Data Backbone Runbook

This runbook covers the implemented Summer League data backbone: raw NBA Stats
fetching, raw audit, normalized backfill, player resolution, and final QA.

## Status

- Implementation merged in PR `#343`.
- Merge commit: `80255e20846a195af25c474076945cacafeec9ce`.
- Final QA report: `docs/qa/summer-league-backbone-qa-2026-06-10.md`.
- Required stress slices passed with `0` errors:
  - `2024/15` Las Vegas
  - `2024/13` California Classic
  - `2010/14` Orlando
  - `2007/15` Las Vegas

## Prerequisites

Run commands through Conda:

```bash
conda run -n draftguru --no-capture-output <command>
```

For DB-backed commands, set at minimum:

```bash
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test
export SECRET_KEY=test-secret
export ENV=dev
```

For integration tests against the same disposable database, also set:

```bash
export TEST_DATABASE_URL="$DATABASE_URL"
export PYTEST_ALLOW_DB=1
export PYTEST_ALLOW_TEST_DB_EQUALS_DATABASE_URL=1
```

Never point test or final-QA backfills at a production database.

## Disposable Database

Start a local Postgres database with pgvector:

```bash
docker run --rm -d \
  --name draftguru-summer-league-qa-pg \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=draftguru_test \
  -p 55432:5432 \
  pgvector/pgvector:pg16
```

Apply migrations:

```bash
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test \
SECRET_KEY=test-secret \
ENV=dev \
conda run -n draftguru --no-capture-output alembic upgrade head
```

Stop the disposable database when finished:

```bash
docker stop draftguru-summer-league-qa-pg
```

## Raw Fetch

Fetch modern stress slices:

```bash
conda run -n draftguru --no-capture-output python scripts/fetch_summer_league_raw.py \
  --year 2024 \
  --league-id 15,13 \
  --out-dir data/raw/nba_stats/summer_league \
  --delay 0.35 \
  --retries 2 \
  --retry-delay 1 \
  --verbose
```

Fetch historical stress slices:

```bash
conda run -n draftguru --no-capture-output python scripts/fetch_summer_league_raw.py \
  --year 2010 \
  --league-id 14 \
  --out-dir data/raw/nba_stats/summer_league \
  --delay 0.35 \
  --retries 2 \
  --retry-delay 1 \
  --verbose
```

```bash
conda run -n draftguru --no-capture-output python scripts/fetch_summer_league_raw.py \
  --year 2007 \
  --league-id 15 \
  --out-dir data/raw/nba_stats/summer_league \
  --delay 0.35 \
  --retries 2 \
  --retry-delay 1 \
  --verbose
```

Raw files are written under `data/raw/nba_stats/summer_league/`, which is
gitignored.

## Backfill

Run each stress slice against the disposable DB:

```bash
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test \
SECRET_KEY=test-secret \
ENV=dev \
conda run -n draftguru --no-capture-output python scripts/backfill_summer_league_backbone.py \
  --year 2024 \
  --league-id 15 \
  --raw-root data/raw/nba_stats/summer_league \
  --create-stubs \
  --report-path docs/qa/summer-league-backfill-2024-15.json
```

Repeat with `--year 2024 --league-id 13`,
`--year 2010 --league-id 14`, and `--year 2007 --league-id 15`, changing the
report filename to match the slice. Use `--force` when intentionally refreshing
an already-audited slice.

The backfill command audits raw files, normalizes competitions/teams/games/team
logs, normalizes source players and player game logs, resolves players, and
writes a JSON report.

## QA Harness

Run the final stress-slice QA harness:

```bash
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test \
SECRET_KEY=test-secret \
ENV=dev \
conda run -n draftguru --no-capture-output python scripts/qa_summer_league_backbone.py \
  --slice 2024/15 \
  --slice 2024/13 \
  --slice 2010/14 \
  --slice 2007/15 \
  --raw-root data/raw/nba_stats/summer_league \
  --report-path docs/qa/summer-league-backbone-qa-YYYY-MM-DD.md
```

The command exits nonzero for blocking findings. A shippable run has `0` error
findings.

## Accepted Warnings

These warning classes are accepted when the report evidence matches the final QA
pattern:

- `RAW_RUN_INCOMPLETE`, `RAW_RUN_ERRORS_RECORDED`, and `RAW_FILE_MISSING` for
  optional historical game-detail endpoints only, such as one missing
  `playbyplayv2` file for `2010/14` game `1421000004`.
- `NORMALIZATION_PLAYER_LOG_COUNT_MISMATCH` when comparing season
  `leaguegamelog_player` rows to boxscore-derived normalized player logs.
  Boxscore rows are the product source for player game logs.
- `RAW_FILE_ROW_COUNT_MISSING` on generated `manifest.json` files. Manifests do
  not contain NBA Stats result-set row counts by design.

Core raw files, corrupt parseable endpoints, orphaned normalized rows, duplicate
stable IDs, and unresolved inconsistent canonical links remain blocking errors.

## Verification Commands

Before shipping changes to this pipeline, run:

```bash
conda run -n draftguru --no-capture-output make precommit
```

```bash
conda run -n draftguru --no-capture-output mypy app --ignore-missing-imports
```

```bash
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test \
SECRET_KEY=test-secret \
ENV=dev \
conda run -n draftguru --no-capture-output python -m pytest tests/unit -q
```

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test \
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test \
PYTEST_ALLOW_DB=1 \
PYTEST_ALLOW_TEST_DB_EQUALS_DATABASE_URL=1 \
SECRET_KEY=test-secret \
ENV=dev \
conda run -n draftguru --no-capture-output python -m pytest tests/integration -q
```

```bash
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/draftguru_test \
SECRET_KEY=test-secret \
ENV=dev \
conda run -n draftguru --no-capture-output make coverage.diff TESTS=tests/unit
```

## Troubleshooting

- If a CLI backfill raises SQLAlchemy metadata errors for a foreign key target,
  confirm the relevant schema module is imported before the pipeline uses
  SQLModel metadata.
- If player vector search logs embedding-key errors in a local QA database, the
  resolver falls back to lexical candidates. This is acceptable for disposable
  QA runs unless the ticket specifically validates vector ranking.
- If historical `playbyplayv2` or `shotchartdetail` repeatedly fails but box
  scores normalize, treat the slice as partial and document the endpoint gap in
  the QA report.
