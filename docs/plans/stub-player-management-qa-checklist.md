# Stub Player Management — Product QA Checklist

**Source spec:** `docs/stub_player_management_spec.md`
**Companion:** `docs/plans/stub-player-management-test-plan.md`
**Purpose:** Whole-feature, observable-behavior acceptance criteria. Each item is something a
human (or an agent driving the live app via `/verify`) can confirm against a running instance.
This is the spec-compliance anchor for the project's final QA gate.

**How to exercise:** `make dev` → log in with the admin recipe in
`docs/plans/ai-orchestrator-ticket-spec.md` (`DRAFTGURU_ADMIN_EMAIL` / `DRAFTGURU_ADMIN_PASSWORD`).
All surfaces here are admin-gated under `/admin`.

Legend: each box is a discrete acceptance criterion. `[obs]` = observable in UI, `[data]` =
verify via DB/admin reflection, `[neg]` = negative/guard case that must be *blocked*.

---

## A. Stub creation entry points

### A1 — Quick-add stub (name-only)
- [ ] `[obs]` An "Add stub" action is reachable from the Stubs tab (and the player list) and opens a name-only form/modal (`display_name` required, `draft_year` optional).
- [ ] `[data]` Submitting a brand-new, specific name (≥2 tokens) creates a `PlayerMaster` row with `is_stub = true`, a matching alias row, and a lifecycle row — same shape as an ingestion-minted stub.
- [ ] `[neg]` Submitting a name that **uniquely** matches an existing player is **blocked** (no row created); the UI names the existing player and offers "open it / add as alias instead."
- [ ] `[neg]` Submitting a name that matches **multiple** players is **blocked**; the UI shows the ambiguous candidates and requires the admin to choose or override — it never silently creates.
- [ ] `[neg]` Submitting a single-token / too-vague name (fails the `_can_create_stub_player` ≥2-token guard) is rejected with the guard's reason surfaced (not a silent failure).
- [ ] `[obs]` After a successful quick-add, the new stub appears in the Stubs tab list.

### A2 — `is_stub` checkbox on the full new-player form
- [ ] `[obs]` The new-player form (`/admin/players/new`) has an `is_stub` checkbox in Basic Info, default **unchecked**.
- [ ] `[data]` Creating a player with the box **checked** persists `is_stub = true` (plus the normal lifecycle/status rows); creating with it unchecked persists `is_stub = false` (unchanged default behavior).
- [ ] `[data]` A stub created this way may carry richer fields (school, draft_year) and still shows in the Stubs tab.

### A3 — Board-building stub creation
- [ ] `[obs]` On board detail, typing a name into the add-entry autocomplete that returns **no match** surfaces an inline "Create stub for '<name>'" affordance.
- [ ] `[data]` Using that affordance mints a stub (with dedup pre-check) **and** assigns it to a new board entry in one step — no separate "add unresolved → mint stub" round trip.
- [ ] `[neg]` The inline create runs the same dedup pre-check: if the typed name actually matches an existing player, it steers the admin to that player rather than minting a duplicate.
- [ ] `[obs]` The pre-existing unresolved-entry mint-stub button still works (regression check).

---

## B. Stubs admin tab

- [ ] `[obs]` A **Stubs** nav item appears under "Data Management" in the admin sidebar, gated on the `players` dataset permission (hidden for users without it).
- [ ] `[obs]` `/admin/players/stubs` renders a table of stub players only (`is_stub = true`), with columns: name (links to player detail), school, draft year, position/height/weight (when present), enrichment status, created_at, and a row actions menu.
- [ ] `[obs]` Enrichment-status column renders one of: **Not attempted / Enriching… / Enriched / Failed**, derived from `enrichment_attempted_at` + latest enrichment job.
- [ ] `[obs]` Filters work: enrichment status, draft year, and free-text name search each narrow the list; default sort is created_at desc.
- [ ] `[obs]` Pagination works (server-side limit/offset); navigating pages preserves active filters.
- [ ] `[obs]` Bulk selection (row checkboxes) enables a toolbar with "Enrich selected" and "Delete selected".
- [ ] `[neg]` "Delete selected" / per-row delete **refuses** to delete a stub that has inbound references (news mentions, board entries, etc.) and directs the admin to merge instead; only truly-orphan stubs delete.
- [ ] `[obs]` Per-row actions menu exposes: Enrich, Find duplicates, Edit, Promote to full player, Delete.

---

## C. On-demand enrichment (background + poll)

- [ ] `[obs]` Clicking "Enrich" on a single stub returns immediately (no multi-second blocking request) and the row flips to "Enriching…".
- [ ] `[obs]` "Enrich selected" queues all selected stubs and shows a flash like "Queued N stubs for enrichment".
- [ ] `[data]` Each enrich request creates a `PlayerEnrichmentJob` row in `queued` state; a stub that already has an in-flight (`queued`/`running`) job is **not** double-queued.
- [ ] `[obs]` The Stubs tab polls in the background and updates rows in place as jobs finish — "Enriching…" → "Enriched" (or "Failed"); polling stops once no in-flight jobs remain (no infinite polling).
- [ ] `[data]` A completed enrichment populates the stub's empty bio fields (and college stats / reference image / portrait where available) exactly as the cron sweep does, and stamps `enrichment_attempted_at`.
- [ ] `[data]` A failed enrichment marks the job `failed` with an `error_message` and the row shows "Failed"; `enrichment_attempted_at` is still stamped (no infinite retry loop).
- [ ] `[obs]` Bulk enrich respects the per-request cap (e.g. 25) and the flash message states that the cap was applied when the selection exceeds it (no silent truncation).
- [ ] `[data]` Re-enriching an already-attempted stub from the UI is allowed and only fills still-empty fields (does not clobber existing data).
- [ ] `[data]` (Resilience) Jobs left `running` past a staleness window are reclaimable by the next drain / cron backstop — a web-machine restart mid-run does not strand a stub in "Enriching…" forever.

---

## D. Near-duplicate detection + merge

### Find duplicates
- [ ] `[obs]` "Find duplicates" on a stub opens a panel/modal listing candidate players (name, school, similarity score), ranked, **excluding the stub itself**.
- [ ] `[obs]` Each candidate row has a "Merge" button.
- [ ] `[obs]` When there are no plausible candidates, the panel says so clearly (empty state, not an error).

### Merge (reviewed, destructive)
- [ ] `[obs]` Clicking "Merge" first shows a **confirmation/preview** step — no merge happens on the first click.
- [ ] `[obs]` The preview shows a per-table dry-run report (rows to be reassigned / deleted-as-conflict), which name becomes an alias, and which record survives.
- [ ] `[obs]` The admin can flip which record survives (e.g. keep the better-enriched stub) before confirming.
- [ ] `[data]` On confirm, **all** references move from the discarded player to the survivor: aliases, lifecycle, status, content mentions, college stats, combine (anthro/agility/shooting), metric values, similarity (both anchor & comparison sides), board entries, big-board consensus, and image assets.
- [ ] `[data]` Unique-constraint conflicts are handled (the discard's conflicting rows are dropped, not duplicated) and singletons (`player_status`, `player_lifecycle`) keep the survivor's row.
- [ ] `[data]` After merge: the discarded `PlayerMaster` row is gone, the survivor has gained an alias carrying the discarded name, and there are **no orphaned foreign keys** pointing at the deleted id.
- [ ] `[obs]` The survivor keeps its own slug (the public URL of the survivor is unchanged); navigating to the old stub's URL no longer resolves to a live player.
- [ ] `[obs]` A success flash summarizes what was merged (per-table counts from the report).
- [ ] `[neg]` Attempting to merge a record into itself (`keep_id == discard_id`) is rejected.
- [ ] `[neg]` The whole merge is atomic — if it fails partway, no partial reassignment is left behind (transaction rollback).

---

## E. Cross-cutting

- [ ] `[obs]` Every new route (`/admin/players/stubs*`, merge, enrich, status) is permission-gated: a user without `players` `can_view` cannot reach the tab; merge/delete/promote require `can_edit`.
- [ ] `[data]` "Promote to full player" clears `is_stub` and the player thereafter appears in surfaces that exclude stubs (sitemap, trending, X-threads); it does not retroactively alter ingestion behavior.
- [ ] `[obs]` All new UI honors the light-retro design language (`docs/style_guide.md`) — panels, badges, modals match existing admin styling; BEM class names used.
- [ ] `[obs]` (Visual) Screenshots captured for: Stubs tab table, "Enriching…" row state, duplicates modal, merge-confirm/preview modal, and the `is_stub` checkbox on the new-player form.
- [ ] `[data]` (Perf) The Stubs list query uses an index on `is_stub` (Index Scan, not a Seq Scan on `players_master`) on a prod-like branch, and the route stays within the query budget.

---

## Spec-compliance anchors (for the final QA gate)

These map directly to the spec's Goals (§1) — the QA gate should confirm each is deliverable
from the merged work:

1. Deliberate, deduplicated stub **creation** in the new-player flow and board-building flow → §A.
2. Admin **Stubs tab** with table, filters, and per-row quality actions → §B.
3. **On-demand enrichment** of one/many stubs, backgrounded, with UI status → §C.
4. **Near-duplicate candidates + reviewed full merge** that codifies the merge scripts safely → §D.

Non-goals to guard against scope creep (§1): no auto-merge without confirmation; ingestion
mint behavior unchanged; no general two-full-player merge UI entry point.
