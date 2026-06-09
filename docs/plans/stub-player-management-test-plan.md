# Stub Player Management — Build-Time Test Plan

**Source spec:** `docs/stub_player_management_spec.md`
**Companion:** `docs/plans/stub-player-management-qa-checklist.md`
**Purpose:** Automated coverage to write *during* implementation, mapped to the spec's work
slices so `/create-project` can attach each test group to the ticket that builds the behavior.

**Conventions (per `docs/plans/ai-orchestrator-ticket-spec.md`):**
- Run everything via conda: `conda run -n draftguru --no-capture-output python -m pytest <path>`.
- `tests/unit/` — pure logic, no DB. Mirror `app/` layout.
- `tests/integration/` — DB + FastAPI via HTTPX; needs `TEST_DATABASE_URL` + `PYTEST_ALLOW_DB=1`
  (wrap with `scripts/with-db-env.sh`). New schema modules MUST be imported in
  `tests/integration/conftest.py` or their tables won't be created.
- `tests/visual/` — `make visual` → PNGs under `tests/visual/screenshots/`.
- Patch coverage ≥80% on changed `app/` lines (`make coverage.diff`).
- External calls (Gemini, Wikimedia, S3 portrait gen) are **mocked** in tests.

---

## Slice 1 — Schema & migration (`PlayerEnrichmentJob` + `is_stub` index)

**Implements:** spec §4. **Flavors:** unit, integration (roundtrip).

### Integration (`tests/integration/schema/test_player_enrichment_jobs.py`)
- New `PlayerEnrichmentJob` table is created by the conftest (assert insert/select works) —
  confirms the schema module is registered in `tests/integration/conftest.py`.
- A job row persists `state`, `source`, `player_id`, `requested_by_user_id`, timestamps,
  `error_message`; `state` defaults / transitions are storable.
- Indexes exist: `(state, created_at)` for queue claim and `player_id` for status polling.

### Migration roundtrip (manual gate documented in ticket)
- `alembic upgrade head` then `alembic downgrade base` on a disposable DB runs clean.
- Upgrade creates the `PlayerEnrichmentJob` table via
  `SQLModel.metadata.create_all(..., tables=[PlayerEnrichmentJob.__table__])` and adds the
  `players_master` `is_stub` index via `op.create_index` (table **not** dropped/recreated).

---

## Slice 2 — Stub-creation service wrapper + dedup

**Implements:** spec §A1/§A3, §5 (`create_stub_player`). **Flavors:** unit, integration.

### Unit (`tests/unit/services/test_create_stub_player.py`)
- Pre-check returns **create** when no existing match → minted with `is_stub=true`.
- Pre-check returns **blocked-existing** on a unique match (no creation).
- Pre-check returns **ambiguous** on multiple matches (no creation; candidates returned).
- Token guard: single-token / vague name rejected with reason (`_can_create_stub_player`).
- Name normalization parity with the ingestion path (suffixes/diacritics).

### Integration (`tests/integration/services/test_create_stub_player.py`)
- Seed an existing player, then call the wrapper with a colliding name → blocked, DB unchanged.
- Call with a fresh name → `PlayerMaster(is_stub=true)` + alias row + lifecycle row created.

---

## Slice 3 — Quick-add + `is_stub` checkbox routes

**Implements:** §A1, §A2, §6. **Flavors:** integration, e2e (admin), visual.

### Integration (`tests/integration/routes/admin/test_stub_quick_add.py`)
- `POST /admin/players/stubs/quick-add` happy path creates a stub and redirects with success.
- Blocked-existing and ambiguous paths return the guard messaging, create nothing.
- Existing `POST /admin/players` with `is_stub` checked persists `is_stub=true` + lifecycle/status;
  unchecked persists `is_stub=false` (regression on the existing create route).
- Permission gate: non-`players` user is denied.

### Browser e2e (`/verify`, admin login recipe)
- Open Stubs tab → "Add stub" → submit fresh name → row appears.
- Submit a duplicate name → blocked message shown, no new row.

### Visual
- New-player form showing the `is_stub` checkbox; quick-add modal.

---

## Slice 4 — Board add-entry inline stub creation

**Implements:** §A3. **Flavors:** integration, e2e (admin).

### Integration (`tests/integration/routes/admin/test_board_inline_stub.py`)
- Adding a board entry with a no-match name via the inline create mints a stub **and** creates
  a resolved board entry pointing at it (one request).
- Dedup pre-check: a name matching an existing player steers to that player, no new stub.
- Regression: existing unresolved-entry `mint-stub` endpoint still works.

### Browser e2e
- On board detail, type an unknown name → "Create stub for '<name>'" → entry added resolved.

---

## Slice 5 — Stubs tab list + filters + bulk/delete/promote

**Implements:** §B, §5 (`list_players` extension, `promote_stub_to_full`, `delete_stub`).
**Flavors:** integration, e2e (admin), visual, perf.

### Integration (`tests/integration/routes/admin/test_stubs_tab.py`)
- `GET /admin/players/stubs` lists only `is_stub=true` rows.
- Filters: enrichment status, draft year, name search each narrow results; default sort created_at desc.
- Pagination preserves filters.
- Enrichment-status derivation (Not attempted / Enriching… / Enriched / Failed) reflects
  `enrichment_attempted_at` + latest job state.
- `POST .../{id}/promote` clears `is_stub`.
- `POST .../{id}/delete` deletes an orphan stub but **refuses** when inbound references exist
  (assert refusal + row preserved). `count_inbound_references` covered.
- Permission gates (view vs edit) on each route.

### Perf (`tests/integration/perf/`)
- Add `/admin/players/stubs` to the query budget; assert within budget.
- Document `make explain ROUTE=admin-stubs` (prod-like branch) shows Index Scan on the new
  `is_stub` index — manual gate noted in the ticket DoD.

### Visual
- Stubs tab table (populated + empty states); status badges.

---

## Slice 6 — Enrichment refactor + queue + on-demand routes

**Implements:** §C, §5 (`enrich_player` extraction, `enqueue_enrichment`,
`drain_enrichment_queue`, `enrichment_status`). **Flavors:** unit, integration, e2e (admin).

### Unit (`tests/unit/services/test_enrichment_queue.py`)
- `enqueue_enrichment` dedups players with an in-flight job; respects the per-request cap.
- Bulk cap signalling (returns/flags the truncation count).
- Stale-`running` reclaim logic.

### Integration (`tests/integration/services/test_enrichment_queue.py` + `routes/admin/test_stub_enrich.py`)
- Refactor parity: `run_enrichment_sweep` still enriches unattempted stubs after extraction
  (regression — mock external fetch).
- `enrich_player` core: fills empty bio fields, upserts college stats, stamps
  `enrichment_attempted_at`; re-run only fills still-empty fields (idempotent, no clobber).
- `drain_enrichment_queue` transitions `queued → running → succeeded`; on a forced fetch error
  transitions to `failed` with `error_message` and still stamps `enrichment_attempted_at`.
- `POST /admin/players/stubs/enrich` (single + bulk) creates jobs, does not double-queue,
  returns immediately, schedules the background drain.
- `GET /admin/players/stubs/enrichment-status?ids=` returns `{player_id: {state, error}}`.
- Cron backstop: `cron_runner` drains queued jobs (assert wiring).
- Permission gate on enrich/status routes.

### Browser e2e
- Single enrich → row shows "Enriching…" → polling flips to "Enriched" (mock fast path).

---

## Slice 7 — Near-duplicate panel + merge service + merge routes

**Implements:** §D, §5 (`player_merge_service`: `preview_merge`, `merge_players`,
`count_inbound_references`). **Flavors:** unit, integration, e2e (admin), visual.

### Unit (`tests/unit/services/test_player_merge_planning.py`)
- Child-table planning: classifies each FK relation as reassign / conflict-delete / singleton.
- `player_similarity` two-column handling: self-link delete + conflict delete + reassign.
- `keep_id == discard_id` rejected.

### Integration (`tests/integration/services/test_player_merge.py`) — the critical test
- Seed a stub with rows across **every** FK table to `players_master`: aliases, lifecycle,
  status, content mentions, college stats, combine_anthro/agility/shooting, metric values,
  similarity (both anchor & comparison), board entries, big_board_consensus, image assets
  (+ cascade tables: embeddings, pending image previews).
- After `merge_players(keep_id, discard_id)`:
  - Survivor owns all reassignable rows; discard's conflicting rows removed (no unique violations).
  - Singletons resolved to the survivor's row.
  - Discard `PlayerMaster` deleted; survivor gained an alias with the discarded name.
  - No orphaned FKs reference the deleted id (assert per table).
  - Survivor slug unchanged.
- `preview_merge` returns per-table counts matching what `merge_players` then does (dry-run fidelity).
- Atomicity: inject a failure mid-merge → assert full rollback (no partial reassignment).

### Integration (`tests/integration/routes/admin/test_stub_merge.py`)
- `GET .../{id}/duplicates` returns ranked candidates excluding the stub itself; empty state.
- `GET .../merge/preview` renders the dry-run report.
- `POST .../merge` requires confirmation params, executes, flashes the report; `can_edit` gated.

### Browser e2e
- Find duplicates → preview → confirm merge → survivor page intact, stub URL no longer resolves.

### Visual
- Duplicates modal; merge-confirm/preview modal.

---

## Coverage / gating summary

| Gate | Command |
|---|---|
| Unit | `conda run -n draftguru --no-capture-output python -m pytest tests/unit -q` |
| Integration | `scripts/with-db-env.sh conda run -n draftguru --no-capture-output python -m pytest tests/integration -q` |
| Types | `conda run -n draftguru --no-capture-output mypy app --ignore-missing-imports` |
| Lint/format | `conda run -n draftguru --no-capture-output pre-commit run --all-files` |
| Patch coverage ≥80% | `make coverage.diff` |
| Perf budget | `make perf` (+ `make explain ROUTE=admin-stubs` on prod-like branch) |
| Visual | `make visual` → review `tests/visual/screenshots/` |

**Highest-risk area:** Slice 7 merge — destructive and cross-table. The full-FK integration
test + atomicity/rollback test are mandatory and should block merge of that ticket.
