# 2026 Summer League Roster Foundation — Test Plan

**Sources:**
- Pitch: `docs/plans/summer-league-2026-roster-foundation-pitch.md`
- Feature plan (Workstream 0 + A in scope): `docs/plans/summer-league-2026-full-roster.md`
- Schema sketch (ticket-ready DDL): `docs/plans/summer-league-2026-workstream0-schema.md`
- Repo orchestration guide: `docs/plans/ai-orchestrator-ticket-spec.md`

**Sibling artifact:** QA checklist at `summer-league-2026-roster-foundation-qa-checklist.md`

## Purpose

This slice is a backend data-foundation pipeline over NBA.com roster JSON, writing onto the
canonical journey-graph grain. The major risks are: a non-reversible or column-rewriting
migration; a scraper that crashes on the currently-empty roster state; a **non-idempotent
loader that overwrites or duplicates assertions** (the single highest risk — it destroys
the append-only history the whole strategy depends on); resolution that silently mis-links
players; and a schema shape that would force a future restructuring migration. The plan
therefore prioritizes pure-logic unit tests (parsing, diff, supersession) and DB-backed
integration tests (migration up/down, loader idempotency, resolution backfill) over any
browser/visual testing — there is no UI in scope.

Test tiers follow `docs/plans/ai-orchestrator-ticket-spec.md`:

- `tests/unit/` — pure `__NEXT_DATA__` parsing, roster-diff computation, and
  assertion-supersession decision logic, with no DB dependency.
- `tests/integration/` — SQLModel schema/migration, loader upserts against real tables,
  idempotency re-runs, and resolution backfill. Requires `TEST_DATABASE_URL` and
  `PYTEST_ALLOW_DB=1`.
- CLI tests — scraper argument parsing, empty-roster handling, snapshot writing, exit codes.

**Repo gotchas (must honor):**
- New schema modules (`app/schemas/player_affiliation.py`, the participation table) must be
  imported in `tests/integration/conftest.py` so the tables are created for integration runs.
- Run integration tests with `GEMINI_API_KEY= GEMINI_SUMMARIZATION_API_KEY=` to dodge the
  embedding-listener `player_embeddings_pkey` flakiness.
- The `affiliation_status_enum` is shared by both tables — create the PG enum type once in
  the migration to avoid a duplicate-type error.

## Required Build-Time Tests

| Requirement | Test Type | Suggested Test | Ticket |
|---|---|---|---|
| QA checklist + test plan exist and map to the spec | manual docs review | verify links, ticket mappings, acceptance categories | T0 |
| Migration creates both tables, FKs, indexes, uniqueness constraints, and both enum types | integration | `tests/integration/test_player_affiliation_schema.py` | T1 |
| Migration adds nullable `participation_id` + index to `summer_league_player_game_logs` without rewriting existing rows | integration | `test_player_affiliation_schema.py::test_participation_id_added_nullable` | T1 |
| Migration downgrade drops new tables, column, and enum types cleanly | integration | `test_player_affiliation_schema.py::test_downgrade_clean` | T1 |
| Append-only/bitemporal columns present (`recorded_at`, `effective_*`, `supersedes_id`, `superseded_at`, `retracted_at`) | integration | `test_player_affiliation_schema.py::test_assertion_columns` | T1 |
| Participation uniqueness on `(competition, team_entry, source_player, stint)` | integration | `test_player_affiliation_schema.py::test_participation_unique` | T1 |
| `__NEXT_DATA__` roster JSON parses to typed records with PERSON_ID + bio fields | unit | `tests/unit/test_roster_scraper.py::test_parse_next_data_roster` | T2 |
| Venue landing page enumerates all team links + TeamIDs | unit | `test_roster_scraper.py::test_enumerate_team_links` | T2 |
| Empty `roster: []` parses to zero players without error | unit | `test_roster_scraper.py::test_empty_roster_no_crash` | T2 |
| Scraper writes one deterministic raw snapshot per run; re-run safe | unit/CLI | `test_roster_scraper.py::test_snapshot_idempotent_write` | T2 |
| Per-team fetch failure is captured, doesn't abort the run | unit | `test_roster_scraper.py::test_partial_failure_continues` | T2 |
| Roster-diff computes added/unchanged/cut per team | unit | `tests/unit/test_roster_diff.py::test_diff_classification` | T3 |
| Supersession logic: cut → new CUT row referencing prior; add → new ANNOUNCED row | unit | `test_roster_diff.py::test_supersession_decisions` | T3 |
| First load creates one source player per PERSON_ID + one ANNOUNCED assertion + one participation row | integration | `tests/integration/test_roster_loader.py::test_first_load` | T3 |
| Re-load of unchanged roster creates no new assertions/participation (idempotent) | integration | `test_roster_loader.py::test_reload_idempotent` | T3 |
| Late add → exactly one new ANNOUNCED assertion, others unchanged | integration | `test_roster_loader.py::test_late_add` | T3 |
| Dropped player → superseding CUT assertion; prior row retained with `superseded_at` (never deleted) | integration | `test_roster_loader.py::test_drop_supersedes_not_deletes` | T3 |
| Point-in-time roster reconstructable from the assertion stream | integration | `test_roster_loader.py::test_history_reconstruction` | T3 |
| Loader emits a roster-diff report with correct per-team counts | integration | `test_roster_loader.py::test_diff_report` | T3 |
| PERSON_ID with existing `nba_stats` external id → deterministic EXTERNAL_ID resolution | integration | `tests/integration/test_roster_resolution.py::test_external_id_match` | T4 |
| Unmatched player + `--create-stubs` → `is_stub` player linked (STUB); without flag → UNRESOLVED, no guess | integration | `test_roster_resolution.py::test_stub_vs_unresolved` | T4 |
| Resolution backfills `player_id` onto participation and affiliation | integration | `test_roster_resolution.py::test_backfill_canonical_ids` | T4 |

## Manual / Operational Verification

- **Live empty-state smoke (now):** run `scripts/fetch_summer_league_rosters.py` against the
  pilot venue today; confirm it completes and reports zero rostered players (pages are live
  but `roster: []`).
- **Live populated smoke (~Jul 1+):** re-run once teams announce; confirm players load,
  PERSON_IDs resolve deterministically, and the diff report shows the adds. This requires
  network access; it is operational verification, not a CI test.

## Definition of Done (test gate)

- `conda run -n draftguru make precommit` clean.
- `conda run -n draftguru mypy app --ignore-missing-imports` clean.
- `conda run -n draftguru pytest tests/unit -q` green.
- Integration suite green with `GEMINI_API_KEY= GEMINI_SUMMARIZATION_API_KEY=` and a
  disposable DB; migration up **and** down verified on that DB.
- `make coverage.diff` ≥80% patch coverage on changed `app/` lines.
- No e2e/visual required (no UI in this slice).
