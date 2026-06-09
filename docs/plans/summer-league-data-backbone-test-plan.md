# Summer League Data Backbone Test Plan

**Sources:**
- Master spec: `docs/plans/summer-league-data-backbone.md`
- Repo orchestration guide: `docs/plans/ai-orchestrator-ticket-spec.md`
- GitHub master issue: `#319`

**Sibling artifact:** QA checklist at `summer-league-data-backbone-qa-checklist.md`

## Purpose

The Summer League data backbone is a backend data pipeline over NBA.com raw JSON
snapshots. The major risks are silent source-data loss, incorrect row-count
parity, non-idempotent upserts, unresolved player identity blocking ingestion,
and weak final evidence. The test plan therefore prioritizes service-level unit
tests, DB-backed integration tests, CLI contract tests, and a final QA harness
over browser or visual testing.

Test tiers follow `docs/plans/ai-orchestrator-ticket-spec.md`:

- `tests/unit/` for pure parsing, planning, path, checksum, report, and matching
  logic with no DB dependency.
- `tests/integration/` for SQLModel schema, migrations where practical, DB
  upserts, FastAPI app wiring only if future routes are added, and end-to-end
  fixture backfills. Integration tests require `TEST_DATABASE_URL` and
  `PYTEST_ALLOW_DB=1`.
- CLI tests for script argument parsing, dry-run behavior, exit codes, and
  report writing.
- Final QA gate checks for full-project behavior and real stress slices.

No visual or browser testing is required unless later tickets add UI, which is
out of scope for this project.

## Required Build-Time Tests

| Requirement | Test Type | Suggested Test | Ticket Mapping |
|---|---|---|---|
| QA checklist and test plan exist and map to the master spec | manual docs review | verify links, ticket mappings, and acceptance categories | `#320` |
| Archive planner preserves raw relative paths under the S3 prefix | unit | `tests/unit/test_summer_league_archive.py::test_s3_key_preserves_relative_path` | `#321` |
| Archive dry-run reports planned keys without S3 writes | unit | `tests/unit/test_summer_league_archive.py::test_dry_run_skips_uploads` | `#321` |
| Archive reports checksum, byte size, skipped, uploaded, and error counts | unit | `tests/unit/test_summer_league_archive.py::test_archive_report_counts` | `#321` |
| Raw audit schema creates run/file tables, enums, FKs, indexes, and uniqueness constraints | integration | `tests/integration/test_summer_league_raw_schema.py` | `#322` |
| Raw audit schema supports manifest checksum, S3 manifest key, byte size, and parse status fields | integration | `tests/integration/test_summer_league_raw_schema.py::test_raw_audit_metadata_fields` | `#322` |
| Schema module is imported before integration metadata creation | integration | `tests/integration/conftest.py` creates Summer League tables | `#322`, `#324`, `#328` |
| Audit scanner parses manifests, endpoint names, checksums, byte sizes, and row counts | unit | `tests/unit/test_summer_league_audit.py` | `#323` |
| Audit scanner records missing, empty, corrupt, skipped, and parsed files explicitly | unit + integration | fixture-tree audit tests with each status | `#323` |
| Audit upserts are idempotent | integration | `tests/integration/test_summer_league_audit.py::test_audit_rerun_is_idempotent` | `#323` |
| Product schema creates competition, team, game, source-player, team-log, and player-log tables | integration | `tests/integration/test_summer_league_product_schema.py` | `#324` |
| Product schema allows nullable canonical `player_id` while requiring `source_player_id` | integration | `tests/integration/test_summer_league_product_schema.py::test_unresolved_player_log_is_allowed` | `#324` |
| Product schema enforces unique `nba_stats_game_id` and `nba_stats_person_id` | integration | constraint tests in `test_summer_league_product_schema.py` | `#324` |
| Competition/team/game normalization converts source rows and dates/minutes/stats correctly | unit | `tests/unit/test_summer_league_normalization.py` | `#325` |
| Competition/team/game/team-log normalization upserts by stable keys | integration | `tests/integration/test_summer_league_normalization.py` | `#325` |
| Competition quality flags classify full, partial, box-only, and raw-only slices | unit + integration | quality-classification helper tests and fixture backfill checks | `#325` |
| Source-player ingestion preserves `PERSON_ID`, raw name, normalized name, and first/last seen years | integration | `tests/integration/test_summer_league_player_logs.py` | `#326` |
| Player game logs preserve parsed traditional, advanced, and scoring fields | unit + integration | parsing unit tests plus DB row assertions | `#326` |
| Player game-log upserts are idempotent and do not require canonical player links | integration | rerun fixture test in `test_summer_league_player_logs.py` | `#326` |
| Resolution cascade handles external ID, existing source identity, exact name, alias, fuzzy/vector candidates, unresolved, and stub cases | unit + integration | `tests/unit/test_summer_league_player_resolution.py` and `tests/integration/test_summer_league_player_resolution.py` | `#327` |
| Resolution writes `player_external_ids` and can backfill `player_id` onto existing logs | integration | source-player resolution integration tests | `#327` |
| Ambiguous candidates remain reviewable instead of auto-resolving | unit + integration | candidate-threshold tests and unresolved DB assertions | `#327`, `#328` |
| Review queue persists pending reviews, selected players, status, notes, and reviewed timestamp | integration | `tests/integration/test_summer_league_resolution_reviews.py` | `#328` |
| One active pending review per source player is enforced | integration | partial unique index or service-logic test | `#328` |
| End-to-end backfill command plans audit, normalization, player-log ingestion, resolution, and reporting stages | unit | `tests/unit/test_summer_league_backfill.py` | `#329` |
| Backfill service is idempotent over a small fixture tree | integration | `tests/integration/test_summer_league_backfill.py::test_backfill_twice_keeps_counts_stable` | `#329` |
| Backfill dry-run avoids DB mutation where supported and reports unsupported dry-run stages | unit + integration | dry-run planning and DB count comparison | `#329` |
| QA service returns structured findings with severity, code, message, and evidence | unit | `tests/unit/test_summer_league_qa_service.py` | `#330` |
| QA validators catch raw audit gaps, count mismatches, duplicate rows, orphans, unresolved states, and partial-data classifications | integration | `tests/integration/test_summer_league_qa_service.py` | `#330` |
| QA CLI parses stress slices, writes Markdown, and exits nonzero for blocking findings | unit | `tests/unit/test_summer_league_qa_cli.py` | `#331` |
| QA report includes counts, examples, severities, and failure summaries | unit | Markdown rendering tests in `test_summer_league_qa_cli.py` | `#331` |
| Compact fixture trees cover modern, satellite, partial, corrupt, and missing-endpoint cases | unit/integration fixture sanity | `tests/unit/test_summer_league_qa_fixtures.py` and negative-case tests | `#332` |
| Negative QA tests prove validators catch count mismatches, orphaned rows, duplicates, missing endpoints, corrupt raw files, and unresolved-player cases | integration | `tests/integration/test_summer_league_qa_negative_cases.py` | `#332` |
| Full project checks and stress-slice QA pass on the combined feature branch | final QA gate | commands listed in `#333` plus `scripts/qa_summer_league_backbone.py` | `#333` |

## Required Command Coverage By Ticket

| Ticket | Required Command(s) |
|---|---|
| `#320` | Manual docs review only; no code tests required |
| `#321` | `conda run -n draftguru --no-capture-output python -m pytest tests/unit/test_summer_league_archive.py` |
| `#322` | `conda run -n draftguru --no-capture-output python -m pytest tests/integration/test_summer_league_raw_schema.py` |
| `#323` | `conda run -n draftguru --no-capture-output python -m pytest tests/unit/test_summer_league_audit.py tests/integration/test_summer_league_audit.py` |
| `#324` | `conda run -n draftguru --no-capture-output python -m pytest tests/integration/test_summer_league_product_schema.py` |
| `#325` | `conda run -n draftguru --no-capture-output python -m pytest tests/unit/test_summer_league_normalization.py tests/integration/test_summer_league_normalization.py` |
| `#326` | `conda run -n draftguru --no-capture-output python -m pytest tests/unit/test_summer_league_player_logs.py tests/integration/test_summer_league_player_logs.py` |
| `#327` | `conda run -n draftguru --no-capture-output python -m pytest tests/unit/test_summer_league_player_resolution.py tests/integration/test_summer_league_player_resolution.py` |
| `#328` | `conda run -n draftguru --no-capture-output python -m pytest tests/integration/test_summer_league_resolution_reviews.py` |
| `#329` | `conda run -n draftguru --no-capture-output python -m pytest tests/unit/test_summer_league_backfill.py tests/integration/test_summer_league_backfill.py` |
| `#330` | `conda run -n draftguru --no-capture-output python -m pytest tests/unit/test_summer_league_qa_service.py tests/integration/test_summer_league_qa_service.py` |
| `#331` | `conda run -n draftguru --no-capture-output python -m pytest tests/unit/test_summer_league_qa_cli.py` |
| `#332` | `conda run -n draftguru --no-capture-output python -m pytest tests/integration/test_summer_league_qa_negative_cases.py` |
| `#333` | Full repo checks plus stress-slice QA harness, as defined below |

## Final QA Gate

Ticket `#333` must run last on the combined feature branch. It should execute
the repo Definition of Done through Conda:

```bash
conda run -n draftguru make precommit
conda run -n draftguru mypy app --ignore-missing-imports
conda run -n draftguru pytest tests/unit -q
conda run -n draftguru make coverage.diff TESTS=tests/unit
```

Because this project touches schemas, services, scripts, and DB-backed
normalization, final QA must also run the relevant integration suite:

```bash
conda run -n draftguru --no-capture-output python -m pytest tests/integration -q
```

Then run the Summer League QA harness against the required stress slices from
`docs/plans/summer-league-data-backbone.md`:

```bash
conda run -n draftguru --no-capture-output python scripts/qa_summer_league_backbone.py \
  --slice 2024/15 \
  --slice 2024/13 \
  --slice 2010/14 \
  --slice 2007/15 \
  --raw-root data/raw/nba_stats/summer_league \
  --report-path docs/qa/summer-league-backbone-qa-YYYY-MM-DD.md
```

If earlier implementation tickets choose `--year/--league-id` instead of
`--slice`, final QA should adapt the command while preserving the four required
stress slices.

## Fixture Strategy

Build-time tests should not commit or depend on the full local raw scrape.
Instead, add compact fixtures under `tests/fixtures/summer_league/` that model:

- A modern full Vegas-like slice with team/player gamelogs and box endpoints.
- A modern satellite venue slice with fewer games.
- An older partial Orlando-like slice with missing or differently shaped
  endpoints.
- A corrupt JSON file.
- A missing endpoint that should produce partial quality, not a hard failure.
- Ambiguous, unresolved, alias, external-ID, and stub-creation player cases.

The full raw scrape under `data/raw/nba_stats/summer_league/` is reserved for
operator runs and final QA evidence, not ordinary unit or integration fixtures.

## Ticket Injection Notes

- Schema tickets must update `tests/integration/conftest.py` so SQLModel
  metadata includes the new Summer League tables before integration tests call
  `create_all`.
- Migration tickets for new tables should follow the repo guidance:
  `SQLModel.metadata.create_all(bind=..., tables=[...])` in upgrade and matching
  `drop_all` in downgrade.
- Service functions should be stateless and take `AsyncSession` as the first
  parameter when they touch the database.
- CLI wrappers should stay thin; business logic belongs under
  `app/services/summer_league/`.
- QA validators should return structured report DTOs instead of printing
  directly. The CLI/report-writer ticket can decide how to render those DTOs.
- Public UI, admin UI, share cards, derived composite stats, live polling, and
  scheduled jobs remain out of scope.
