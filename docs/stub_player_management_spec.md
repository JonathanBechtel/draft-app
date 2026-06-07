# Stub Player Management — Tech Spec

**Status:** Draft for review
**Author:** (DraftGuru)
**Date:** 2026-06-05
**Feeds into:** `/create-qa-checklist` → `/create-project` → `/orchestrate`

---

## 1. Problem & Goals

"Stub" players (`PlayerMaster.is_stub = True`) are lightweight rows auto-minted when the
system encounters a player name it can't match to a full record — today via the **news/feed
ingestion pipeline** (`player_mention_service._create_stub_player`) and via **board entry
resolution** (`board_service.mint_stub_for_entry`). They carry only a `display_name` (plus
parsed name parts) until the cron **enrichment sweep** fills them in.

The gaps this spec closes:

1. **No first-class way to create a stub.** Admins can mint a stub only as a side effect of
   resolving an unresolved board entry. There's no place to deliberately create one when
   adding a new player or building a board.
2. **No place to see stubs.** Stubs are scattered through the general player list with no
   dedicated view, no enrichment status, and no quality controls.
3. **Enrichment is cron-only.** `run_enrichment_sweep` runs hourly over *all* unenriched
   stubs. Admins can't enrich a specific stub on demand.
4. **Duplicate stubs accumulate invisibly.** Weak name-matching has repeatedly created
   duplicate players (consensus dup-player cleanup, BBRef namesake contamination). Cleanup
   has only ever happened via one-off scripts (`scripts/top100/merge_players.py`). There's no
   reviewed, in-app merge action.

### Goals

- Make stub **creation** a deliberate, deduplicated action available in the new-player flow
  and the board-building flow.
- Add an admin **Stubs tab**: table view with filters and per-row quality actions.
- Allow **on-demand enrichment** of one or many stubs, run in the background with the UI
  reflecting status.
- Surface **near-duplicate candidates** for a stub and provide a reviewed **full merge**
  action that codifies the existing merge scripts as a safe in-app operation.

### Non-goals (this feature)

- Auto-merging without explicit admin confirmation (violates the entity-resolution
  philosophy: ambiguous matches stay unresolved, never guessed).
- Changing how ingestion mints stubs (the upstream matchers are unchanged).
- A general player-merge UI for two *full* players (the merge service will support it, but
  the entry point in this feature is stub-centric).

---

## 2. Reuse Map — what already exists

This feature is **mostly UI wiring over existing services**. Agents should reuse, not rebuild:

| Capability | Existing code | Reuse plan |
|---|---|---|
| Mint a stub from a name | `player_mention_service._create_stub_player`, `resolve_player_names` | Promote a thin public `create_stub_player(db, full_name, draft_year)` wrapper (with dedup pre-check) for admin use |
| Dedup pre-check before minting | `player_mention_service.find_existing_player(db, name) -> (match, ambiguous)` | Call before quick-add to warn/block on existing match |
| Near-duplicate candidates | `player_search_service.find_candidate_players(db, query, k)` (trigram + pgvector hybrid) | Drive the "find duplicates" panel |
| Enrichment (bio/stats/image/portrait) | `player_enrichment_service.run_enrichment_sweep(session_factory)` | Refactor to extract a single-player core; sweep keeps looping it |
| Stub list + filters | `admin_player_service.list_players(...)` | Extend with `is_stub`/enrichment filters |
| Player → canonical merge | `scripts/top100/merge_players.py` (`_merge_child_table`, `_ensure_alias`, full child-table specs) | Codify into `player_merge_service` and call from an admin route |
| Background work in-app | FastAPI `BackgroundTasks` (see `routes/admin/users.py` → `email_worker.send_pending_emails`) | Trigger the enrichment-queue worker |
| Cron entry | `app/cli/cron_runner.py` (Fly scheduled machine, hourly) | Add queue-drain as a cron backstop |
| Job-status-as-DB-row pattern | `ImageBatchJob` in `app/schemas/image_snapshots.py` (state enum + indexes) | Mirror for `PlayerEnrichmentJob` |
| Admin nav/tabs | `app/templates/admin/base.html` (`active_nav`, permission gating) | Add a "Stubs" nav item under Data Management |

---

## 3. Feature Breakdown

### A. Stub creation entry points

**A1. Quick-add stub (name-only).**
A small "Add stub" action (modal or mini-form) reachable from the Stubs tab and the player
list. Input: `display_name` (required), optional `draft_year`.

- On submit, **always run a dedup pre-check** via `find_existing_player(db, name)`:
  - **Unique match found** → block creation, show "This looks like an existing player: *X* —
    open it / add as alias instead." (Do not silently create.)
  - **Ambiguous (multiple matches)** → block, show the candidates, require the admin to pick
    or override.
  - **No match** → create via the `create_stub_player` wrapper (mints `PlayerMaster(is_stub=True)`
    + alias + lifecycle, same as the ingestion path).
- Respect the existing `_can_create_stub_player` guard (≥2 normalized tokens) and surface its
  rejection reason rather than failing silently.

**A2. `is_stub` checkbox on the full new-player form.**
Add an `is_stub` checkbox to `app/templates/admin/players/form.html` (default unchecked).
When checked, the created `PlayerMaster.is_stub = True`. This lets an admin create a richer
stub (e.g. with a school/draft-year already known) through the normal form. The full form
already creates lifecycle/status rows; setting the flag is the only change.

**A3. Board-building stub creation (extend existing).**
Board detail already has **mint-stub for unresolved entries**
(`POST /admin/boards/{board_id}/entries/{entry_id}/mint-stub`). Extend the **add-entry**
autocomplete (`boards-detail.js` against `/players/search`) so that when a typed name returns
no match, the admin gets an inline **"Create stub for '<name>'"** affordance that mints a stub
and assigns it to the new entry in one step (reusing the `create_stub_player` wrapper +
dedup pre-check). This removes the current two-step "add unresolved → mint stub" dance for
names the admin knows are new.

### B. Stubs admin tab

New nav item **Stubs** (`/admin/players/stubs`) under Data Management, gated on the `players`
dataset `can_view`/`can_edit` permissions (same as the player list).

**Table columns:** display name (link to player detail), school, draft year, position/height/
weight (from lifecycle/status if present), **enrichment status** (Not attempted / Enriching… /
Enriched / Failed — derived from `enrichment_attempted_at` + latest `PlayerEnrichmentJob`),
created_at, and a per-row actions menu.

**Filters:** enrichment status, draft year, "has duplicates flagged" (optional), free-text
name search. Sort by created_at desc by default. Server-side pagination (reuse
`list_players` limit/offset).

**Bulk selection:** checkboxes + a toolbar with **Enrich selected** (see C) and **Delete
selected** (hard delete of orphan stubs — only stubs with no inbound references; reuse the
merge service's reference-count helper to refuse deletion otherwise).

**Per-row actions:** Enrich, Find duplicates (→ merge), Edit (existing player form),
Promote to full player (clears `is_stub`), Delete.

### C. On-demand enrichment (background + poll)

**Design decision:** there is no in-process task queue. We mirror the `ImageBatchJob`
pattern: enrichment requests create **job rows**; a lightweight worker drains them; the UI
polls job state. Triggering the worker via `BackgroundTasks` gives immediate processing, and
a **cron backstop** (added to `cron_runner.py`) guarantees jobs still complete if the web
machine restarts mid-run.

**Flow:**
1. Admin clicks **Enrich** (single) or **Enrich selected** (bulk) → `POST /admin/players/stubs/enrich`
   with one or more `player_id`s.
2. Route inserts one `PlayerEnrichmentJob` row per player in state `queued` (dedup: skip
   players that already have an in-flight `queued`/`running` job), then schedules
   `background_tasks.add_task(drain_enrichment_queue, SessionLocal, limit=N)` and redirects
   back with a flash ("Queued N stubs for enrichment").
3. `drain_enrichment_queue` claims `queued` jobs (`FOR UPDATE SKIP LOCKED`), sets `running`,
   calls the extracted single-player enrichment core, then sets `succeeded`/`failed` with
   `completed_at` and `error_message`. Each player's `enrichment_attempted_at` is stamped by
   the core exactly as the sweep does today.
4. Stub rows show **Enriching…** while a job is `queued`/`running`. The Stubs tab JS polls
   `GET /admin/players/stubs/enrichment-status?ids=...` (returns `{player_id: {state, error}}`)
   on an interval and updates rows in place; stops when no in-flight jobs remain.
5. **Cron backstop:** `cron_runner` calls `drain_enrichment_queue` (bounded batch) before/after
   the existing `run_enrichment_sweep`, so stragglers always finish.

**Refactor required:** extract the per-player body of `run_enrichment_sweep` into
`enrich_player(db, player) -> SingleEnrichmentResult` (fetch outside txn → apply in short txn
→ portrait in separate txn, preserving the current transaction boundaries). `run_enrichment_sweep`
becomes a loop over `enrich_player`. `drain_enrichment_queue` calls the same core.

### D. Near-duplicate detection + full merge

**Find duplicates:** per-stub action → `GET /admin/players/stubs/{player_id}/duplicates`.
Calls `find_candidate_players(db, query=stub.display_name, k=5)`, **excludes the stub itself**,
returns candidates with names, school, and similarity score. Rendered in a panel/modal that
shows each candidate with a **Merge** button. (Optional enhancement: a tab-wide "scan all
stubs for likely dups" report — deferred, see §10.)

**Merge:** `POST /admin/players/stubs/merge` with `{keep_id, discard_id}` (survivor +
absorbed). The route:
1. Loads both players; validates `keep_id != discard_id` and both exist.
2. Renders a **confirmation step** first via `GET .../merge/preview` showing a dry-run report
   (per-table counts of rows reassigned / deleted-as-conflict), the discard name that becomes
   an alias, and which fields survive. **No merge happens without explicit confirmation.**
3. On confirm, calls `player_merge_service.merge_players(db, keep_id=, discard_id=,
   performed_by=)` inside `async with db.begin()`.

**`player_merge_service`** codifies `scripts/top100/merge_players.py`:
- For each child table (see §4 FK map), reassign `discard_id → keep_id`, handling **unique
  conflicts** (delete the discard's conflicting rows) and **singletons** (`player_status`,
  `player_lifecycle`: keep survivor's row, delete discard's).
- `player_similarity` special-cased on **both** `anchor_player_id` and `comparison_player_id`,
  deleting self-links and conflicts before reassigning.
- `player_embeddings`, `pending_image_previews` are `ON DELETE CASCADE` → drop with the row.
- Insert an alias on `keep_id` from the discard's `display_name` (context `"admin_merge_discard"`,
  `ON CONFLICT DO NOTHING`), mirroring `_ensure_alias`.
- Delete the discard `PlayerMaster` row last. Survivor keeps its slug; the discard's slug is
  freed.
- Return a `MergeReport` (per-table counts) for the flash message + audit.

**Merge direction default:** the stub is the `discard_id` and the chosen candidate is the
`keep_id`, but the preview shows which is which and lets the admin flip survivor (e.g. when
two stubs match and the better-enriched one should win).

---

## 4. Data Model

### New table: `PlayerEnrichmentJob` (`app/schemas/player_enrichment_jobs.py`)

Mirror `ImageBatchJob`'s shape/indexing.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `player_id` | int FK → players_master.id | indexed |
| `state` | str/enum | `queued` / `running` / `succeeded` / `failed`; **indexed** for queue claim |
| `source` | str | `admin_single` / `admin_bulk` / `cron` |
| `requested_by_user_id` | Optional[int] FK → users | null for cron |
| `created_at` / `started_at` / `completed_at` | datetime | |
| `error_message` | Optional[str] | |

Index on `(state, created_at)` for FIFO queue claim; index on `player_id` for status polling.

### `players_master` index

Add an index supporting `WHERE is_stub = true` ordered by `created_at` for the Stubs tab
(partial index `ix_players_master_is_stub_created` on `(is_stub, created_at)` or partial
`WHERE is_stub`). **Verify with `make explain ROUTE=admin-stubs` against a prod-like branch**
(dev volume Seq-Scans regardless) per the perf guard workflow.

### Migration

One Alembic revision: `create_all` for `PlayerEnrichmentJob` (new table → use
`SQLModel.metadata.create_all(..., tables=[PlayerEnrichmentJob.__table__])` in upgrade,
`drop_all` in downgrade) **plus** `op.create_index` for the `is_stub` index on the existing
`players_master` table (do **not** drop/recreate the table). Add the schema module under
`app/schemas/` so Alembic + the integration conftest discover it (remember to register it in
`tests/integration/conftest.py`).

---

## 5. Services

| Service | New / changed | Signature (sketch) |
|---|---|---|
| `player_mention_service` | **new public wrapper** | `async def create_stub_player(db, full_name, *, draft_year=None) -> StubCreateResult` — wraps dedup pre-check + `_create_stub_player`; returns created / blocked-existing / ambiguous |
| `player_enrichment_service` | **refactor** | extract `async def enrich_player(db, player) -> SingleEnrichmentResult`; `run_enrichment_sweep` loops it |
| `player_enrichment_service` (or new `enrichment_queue_service`) | **new** | `async def enqueue_enrichment(db, player_ids, *, source, user_id) -> list[int]`; `async def drain_enrichment_queue(session_factory, *, limit) -> DrainResult`; `async def enrichment_status(db, player_ids) -> dict[int, JobStatus]` |
| `player_merge_service` | **new** | `async def preview_merge(db, *, keep_id, discard_id) -> MergeReport` (dry-run counts); `async def merge_players(db, *, keep_id, discard_id, performed_by) -> MergeReport`; `async def count_inbound_references(db, player_id) -> dict[str,int]` (for safe-delete) |
| `admin_player_service` | **extend** | `list_players(..., is_stub: Optional[bool]=None, enrichment_status: Optional[str]=None)`; add `promote_stub_to_full(db, player_id)` (clears `is_stub`); `delete_stub(db, player_id)` (guarded by reference count) |
| `player_search_service` | reuse as-is | `find_candidate_players(db, query, k)` for the duplicates panel |

All services stateless, `AsyncSession` first param, routes own commits (except `drain_*`,
which owns its own sessions via the factory, like `email_worker`).

---

## 6. Routes (`app/routes/admin/players.py` unless noted)

| Method + Path | Purpose |
|---|---|
| `GET /admin/players/stubs` | Stubs tab (table + filters + bulk toolbar) |
| `POST /admin/players/stubs/quick-add` | Quick-add stub (dedup pre-check) |
| `POST /admin/players/stubs/enrich` | Enqueue enrichment for one/many `player_id`s + schedule background drain |
| `GET /admin/players/stubs/enrichment-status` | Poll job state for `?ids=` (JSON) |
| `GET /admin/players/stubs/{player_id}/duplicates` | Near-duplicate candidates (JSON or partial) |
| `GET /admin/players/stubs/merge/preview` | Dry-run merge report (`keep_id`, `discard_id`) |
| `POST /admin/players/stubs/merge` | Execute merge (confirmed) |
| `POST /admin/players/stubs/{player_id}/promote` | Clear `is_stub` |
| `POST /admin/players/stubs/{player_id}/delete` | Delete orphan stub (reference-guarded) |
| `is_stub` checkbox | handled by existing `POST /admin/players` create route |
| board add-entry "create stub" | extend `POST /admin/boards/{board_id}/entries` (or a small companion route) to mint+assign |

Thin routes; all logic in services. Set `response_model`/`status_code` on JSON endpoints,
raise `HTTPException(404)` for missing players, gate every route on the `players` dataset
permission (merge/delete/promote require `can_edit`).

---

## 7. Templates & Static Assets

- `app/templates/admin/players/stubs.html` — extends `admin/base.html`, `active_nav="stubs"`;
  table, filters, bulk toolbar, duplicates modal, merge-confirm modal.
- `app/templates/admin/players/form.html` — add the `is_stub` checkbox (Basic Info section).
- `app/templates/admin/base.html` — add the **Stubs** nav item under Data Management
  (permission-gated on `players`).
- `app/static/stubs-admin.js` — enrichment polling, bulk-select, quick-add modal, duplicates
  fetch + merge-confirm. Kebab-case file, BEM classes, `DOMContentLoaded` init (per frontend
  conventions). Reuse the autocomplete/poll patterns from `boards-detail.js`.
- `app/static/boards-detail.js` — add the inline "create stub for '<name>'" affordance to the
  add-entry autocomplete.
- Styles in existing `app/static/css/admin.css` (badges for enrichment status, modal styles).

---

## 8. Data-Flow Summary

```
CREATE
  Quick-add ─┐
  Board add ─┼─► find_existing_player (dedup) ─► create_stub_player ─► PlayerMaster(is_stub=True)
  Full form ─┘                                     (+ alias + lifecycle)        + is_stub checkbox

ENRICH
  Enrich btn ─► enqueue_enrichment ─► PlayerEnrichmentJob(queued)
                                          │
              BackgroundTasks ──► drain_enrichment_queue ─► enrich_player (Gemini+Wikimedia+portrait)
              Cron backstop  ──┘                              └─► stamps enrichment_attempted_at, job=succeeded/failed
  UI poll  ─► enrichment-status ◄── job state

DEDUP + MERGE
  Find duplicates ─► find_candidate_players ─► [candidates]
  Merge (confirm) ─► preview_merge (dry-run) ─► merge_players
                       reassign all child tables (conflict/singleton-aware)
                       + alias from discard name + delete discard row
```

---

## 9. Edge Cases & Decisions

- **Dedup never auto-resolves.** Quick-add blocks on unique/ambiguous matches; merge always
  requires confirmation. (Entity-resolution philosophy.)
- **Merge is destructive & hard to reverse.** Always preview first; wrap in one transaction;
  return a per-table report; record `performed_by`. Consider logging the full report for audit.
- **Survivor field precedence:** survivor (`keep_id`) keeps its scalar bio fields. If the stub
  has data the survivor lacks, that is **not** auto-copied in v1 (note in preview; manual edit
  after). Flag as a possible enhancement.
- **Enrichment idempotency:** `enrich_player` still skips/stamps via `enrichment_attempted_at`;
  re-enriching an already-attempted stub from the UI is allowed (explicit admin intent) — the
  job re-runs the fetch and only fills empty fields, matching current sweep behavior.
- **Bulk enrich sizing:** cap the per-request batch (e.g. 25) and let the cron backstop finish
  the rest; surface the cap in the flash message (no silent truncation).
- **Web-machine restart mid-drain:** jobs left in `running` past a timeout are reclaimed by the
  next drain (treat stale `running` as re-claimable, like image batch reset).
- **Promote-to-full:** clearing `is_stub` should not retroactively change ingestion behavior;
  it only affects the filters that exclude stubs (sitemap, trending, X-threads).
- **Safe delete:** refuse to delete a stub with inbound references (news mentions, board
  entries, etc.); direct the admin to merge instead.

---

## 10. Out of Scope / Deferred

- Tab-wide "scan all stubs for duplicate clusters" batch report (start with per-stub on-demand).
- Merging two **full** players from a general UI (service supports it; no entry point yet).
- Field-level conflict resolution UI during merge (survivor-wins in v1).
- An undo/rollback for merges (mitigated by preview + transaction; revisit if needed).

---

## 11. Test Plan

**Unit (`tests/unit`):**
- `create_stub_player` dedup branches (unique-block, ambiguous-block, create, token-guard).
- Name-normalization reuse paths.
- Merge child-table planning logic (conflict vs reassign vs singleton) on fixture rows.

**Integration (`tests/integration`, hits Postgres):**
- Quick-add endpoint: creates stub; blocks on existing; ambiguous path.
- `is_stub` checkbox on create route persists the flag + lifecycle/status rows.
- Board add-entry "create stub" mints + assigns in one step.
- Enrich endpoint: enqueues jobs, dedups in-flight, `drain_enrichment_queue` transitions
  `queued→running→succeeded/failed`, stamps `enrichment_attempted_at`; status endpoint reflects
  state. (Mock the Gemini/Wikimedia/portrait calls.)
- **Merge:** seed a stub with rows across every FK table (aliases, lifecycle, status, mentions,
  college_stats, combine_*, metric_values, similarity on both columns, board_entries,
  big_board_consensus, image assets); assert post-merge that survivor owns all rows,
  conflicts/singletons handled, discard row gone, alias created, no orphaned FKs. Cover the
  unique-conflict and singleton collision cases explicitly.
- Permission gating on every new route.

**Coverage:** ≥80% patch coverage on changed `app/` lines (`make coverage.diff`).

**Visual (`make visual`):** Stubs tab table, enrichment "Enriching…" state, duplicates modal,
merge-confirm modal.

**Perf (`make perf` + `make explain ROUTE=admin-stubs`):** confirm the stub-list query uses the
new `is_stub` index (Index Scan, not Seq Scan on `players_master`) on a prod-like branch; keep
the route within query budget (or consciously bump `tests/integration/perf/budgets.py`).

---

## 12. Definition of Done

`make precommit` clean · `mypy app --ignore-missing-imports` clean · unit + integration green ·
`make coverage.diff` ≥80% · `make visual` reviewed · `make perf`/`make explain` for the new
list query · Alembic revision tested `upgrade head` → `downgrade base` on a disposable DB.

---

## 13. Open Questions

1. **Audit trail for merges** — log `MergeReport` to a table, or just structured logs + flash?
2. **Bulk enrich cap** — 25 per request reasonable, or higher given cron backstop?
3. **Stubs tab home** — sub-route of `/admin/players` (shares permission) vs. top-level nav
   item? (Spec assumes a nav item that points at `/admin/players/stubs`.)
4. **Promote-to-full UX** — silent flag clear, or open the full edit form pre-filled to encourage
   completing the bio?
