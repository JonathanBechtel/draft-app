# Summer League Remediation Roadmap

**Status:** Sequencing plan. Converts the retrospective doc set into ordered, shippable phases.

**Reads with:** `north-star-architecture.md` (principles and doc map). Each phase below points at
the spec that owns its detail — this doc is the order, not the design.

## Sequencing rules

Three rules produced this order. They are worth stating because they explain the non-obvious
placements.

1. **Guardrails are sprinkled through, never batched into a phase.** Each rule lands with — and
   ideally *before* — the work it constrains. A file-size rule that arrives in the last phase let
   every prior phase grow files freely. Import contracts for packages that do not yet exist are
   free and permanent; the same contracts written *after* the package is built are retrofits
   against code that already drifted.

   | Guardrail | Lands in | Protects |
   |---|---|---|
   | Import contracts 2, 3, 5 | 0 | the packages Phases 2 and 4 create |
   | Unscoped-`delete()` checker | 0 | P2, across every later phase |
   | Diff-scoped file-size rule | 0 | every phase's file growth |
   | Ruff complexity rules (ratcheted) | 0 | every phase's new code |
   | Runtime network-in-transaction guards | 1 | the operational fixes themselves |
   | Import contract 4 (`event_desk` ↛ SL), baselined | 1 | Desk decoupling; its ignore list is the progress meter |
   | Golden-number parity harness | 2 | the formula consolidation it gates |
   | Stat-constant confinement | 2 | keeps the consolidation from regrowing |
   | `DatedVersionMixin` (type-level) | 1 | P2, by construction — defined *with* the version-flip tables it shapes, not retrofitted after them |
2. **Plumbing and payoff separate.** Where one change both fixes an operational fault and unlocks
   a product capability, the change lands in the operational phase and the capability follows.
   This avoids fixing the same thing twice.
3. **Unify computation before relocating it.** Deduplicating the stat math precedes both reading
   materialized values and splitting god files — otherwise duplication gets frozen into tables or
   scattered into more files.

**Reliability-first ordering is an owner decision, not an oversight.** Phases 1–3 (operational +
stat + longitudinal) deliberately precede the org/team-program model: stabilize the plumbing
before expanding on it. The org model (Phase 4) is schema-additive and depends on nothing in
Phases 1–3, so if a concrete second competition lands on the calendar it can be pulled forward
without disturbing this sequence — but by default it waits.

Every phase carries **exit criteria**. The failure record's decisive process defect was
operational acceptance deferred past merge; a phase is not done because its code merged.

---

## Phase 0 — Free guardrails + no-op simplification

**Why first:** zero risk, clears noise, and installs the protections everything downstream needs.

| Item | Spec |
|---|---|
| Import contracts 2, 3, 5 (`app.domain`, `app.services.stats`, spoke independence) — written *before* those packages exist, so they start green | discipline §3.1 |
| Unscoped-`delete()` AST checker — the direct P2 guard | discipline §1.1 |
| Diff-scoped file-size rule (pre-commit warns, CI enforces; net-change evaluation so it cannot block Phase 5's decomposition) — **here, not later, so no phase grows files unchecked** | discipline §1.4 |
| Ruff complexity rules (`C901`, `PLR0915`, `PLR0913`) with `per-file-ignores` baseline | discipline §1.6 |
| Merge-coverage test — every FK to `players_master` must be *classified*: registered for reassignment or intentionally cascade-delete (the manual list has silently drifted as SL tables were added) — **shipped; found 13 unclassified edges, all now registered for reassignment** | discipline §3.4, backlog 4.4 |
| Double-uPER compute fix (`metrics.py:728-732`) | backlog 1.4 |
| Remove dead Explorer branches (`available=False` teams/games subjects) | backlog 5.1 |
| Naming/layout normalization; document the backfill ordering precondition | backlog 5.4, 6.3 |

**Exit:** contracts run in CI and pass; the delete checker fails on a deliberately introduced
violation; no behavior change observable in the app.

---

## Phase 1 — Operational: cron and database reliability

**Why second:** this is the live pain, and the most user-visible failure — updates that did not
land while games were in progress.

**Entry gate — CLOSED.** Incident **#669** (July 22–23) supplied the observed evidence the first
draft of this plan lacked, and the gate's three questions are now answered (full diagnosis in
#669). All three answers changed the plan.

1. **Was the deployed image missing the shipped chunking fixes? Yes — and completely.**
   Production ran `bba2986` (deployed Jul 19 22:39 UTC) until Jul 23 01:52, with no release in
   between; the chunking work merged Jul 20. Not cron-machine drift — the whole app was 3.5 days
   stale. The deployed `write_lock.py` was 26 lines with only the *unbounded* acquire, so the
   Desk's ~95-minute wait was the only behavior that code could produce. **Established.**
   (The Jul 19 deploy's migration step had already blocked for 72 minutes and passed silently —
   the hazard fired three days before it caused an outage.)
2. **Was the holder the mega-transaction? No.** It was `_run_venue`'s whole-venue normalization
   transaction — the PBP *writer*. A bare `CREATE INDEX` requests `SHARE`, which cannot be blocked
   by a reader, and the mega-transaction only *reads* the PBP table; it therefore could not have
   queued the migration. That normalization path is already chunked at HEAD and in production
   since Jul 23. **Established for the negative, probable for the positive ID.**
3. **What blocked reads? Three mechanisms, not one** — chain-wide `ACCESS EXCLUSIVE` accumulation
   from four `ALTER`s on a publicly-read table, pending lock requests queueing later readers, and
   default-sized pool exhaustion making it site-wide. See the rewritten desk §5a; this spec's own
   earlier correction was incomplete in a way that could have licensed an unsafe conclusion.

**Consequences for this phase.** The version-flip is **no longer first**: the evidence exonerates
the path it converts. It stays in Phase 1 for its own reasons — largest surviving critical
section, P2 compliance, and the time axis Phase 3 consumes — but shipping it first would spend
the phase's riskiest change on a path #669 does not indict. Migration safety goes first instead.
The scoping subtlety still holds and is still worth fixing (`compute()` loads and refits the full
historical dataset regardless of scope, so scoping bounds neither compute cost nor transaction
length) — but it is a cost finding, not the lock finding. **That cost finding is only half
fixed.** #694's `metrics_rebuild_gate.py` compares a durable input watermark and skips the
rebuild entirely when nothing upstream changed, which removed the off-season waste — an hourly
24-minute recompute of data unchanged since the last game. But when the watermark *does* move it
calls `rebuild_sl_metrics(db)` unscoped, so the full-pool `compute()` and both unscoped deletes
still run. **In a live event, inputs change every tick, so the expensive path runs every hour —
which is precisely the window this phase's exit criteria measure.** Tracked as #701; do not read
#694 as having closed it.

### Status

**Build work complete; exit criteria await a live window.** All three of Phase 1's remaining
build items have shipped: **version-flip publish** (#697, PR #702), **intra-day compaction**
(#698, PR #703), and **latency-class partitioning** (#699, PR #706) are closed. Combined with the
items already listed below as shipped — migration safety and the readiness probe ·
deploy-freshness alarm · import contract 4 · `DatedVersionMixin` (definition only) ·
metric-model fit-history retention · runtime network/writer-lock guards · ingestion identity
guard · the metrics rebuild gate (unchanged-input skip) — every build item in this phase's table
is now shipped. What is left is verification, not code: the phase's exit criteria require hourly
tick completion measured **inside live-game windows**, and Summer League 2026 ended 2026-07-19, so
closing them needs either the next event or a deliberate staging replay (see the "cannot be met
off-season" note below).

**Moved to Phase 2: in-event metrics scoping (#701).** Still open. The rebuild still does a
full-pool `compute()` whenever the input watermark moves, which in a live event is every tick.
That is real, but it is not Phase 1's to fix. Phase 1 needs the cost **contained** — #699's
partitioning (now shipped) stops the slow class blocking the Desk tick, which is what the exit
criterion actually measures. **Reducing** the cost means splitting `compute()` and `ComputeResult`,
which is the stat engine Phase 2 lifts and consolidates, and it cannot be done safely before
Phase 2's first item — the golden-number parity harness that proves the values did not move. Doing
it here would change how the numbers are computed before the guard that verifies them, and would
touch the same code twice. #701 is not a bespoke fix waiting on its own ticket to land — it is
inherited by whatever class-based engine machinery Phase 2's metric registry (stat-engine §2–3)
introduces once the fit/projection split exists there; there is nothing for #701 to do until that
backbone is built.

**Also open, but a decision rather than work:** auto-deploy on merge to `main`. The freshness
alarm observes and deliberately cannot deploy.

**The exit criteria below cannot be met off-season.** They require tick completion measured
*inside live-game windows* across several overlapping cycles. Summer League ended 2026-07-19, so
closing them needs either the next event or a deliberate staging replay — which is a property of
the criteria, not an outstanding task.

| Item | Spec |
|---|---|
| Runtime guards: no network I/O while a transaction is open or the writer lock is held (ContextVar + client checks; hard-fail dev/test, warn+stack in prod) — **SHIPPED** (#692 via PR #696): `app/utils/network_guard.py`; transaction depth from SQLAlchemy `after_begin`/`after_transaction_end`, writer-lock depth marked at both acquire sites in `write_lock.py`, and the guard *called* from the NBA-stats client rather than merely defined | discipline §2.1–2.2 |
| `DatedVersionMixin` (`version` / `registry_version` / `calculation_version` / `is_current` / `as_of`) — defined **here** so the version-flip tables inherit it from day one — **SHIPPED**: definition only in `app/schemas/base.py`; nothing adopts it and no table is created (verified by autogenerate against a scratch DB). Process time is deliberately excluded so job-run time cannot be rendered as a user-facing "as of" (P4) | alignment §5b |
| **Version-flip publish replacing the mega-transaction** — build the new metrics version outside any lock, materialize variants, flip the pointer in a tiny transaction (`app/cli/summer_league_ingest_runner.py:1308-1348`) — **SHIPPED** (#697 via PR #702) | desk §5, stat-engine §5 |
| Intra-day compaction policy for hourly rebuild versions (retain daily close + current) — lands with the version-flip so retention is bounded from the first event — **SHIPPED** (#698 via PR #703): `app/services/summer_league/metric_compaction.py` | stat-engine §5 |
| Stop deleting `SummerLeagueMetricModel` (auditable fit history) — **SHIPPED**: publishing deactivates prior fits instead of wiping the table; upsert on `model_version` keeps a rebuild re-runnable. Removes one of three P2 waivers in `metrics.py`; the remaining two are the projection tables the version-flip retires | stat-engine §5 |
| Latency-class partitioning: fast live poller / medium projection builder / slow backbone — **SHIPPED** (#699 via PR #706): fast/medium/slow Desk tick classes, per-class Fly cron machines gated behind promotion | desk §2 |
| **Migration safety** (from #669) — **ship this first.** Short `lock_timeout`; `transaction_per_migration=True` so a blocked revision cannot hold earlier revisions' `ACCESS EXCLUSIVE` locks chain-wide (the entry gate's mechanism #1 — `lock_timeout` alone does not bound lock *lifetime*); `CREATE INDEX CONCURRENTLY` via `autocommit_block()`; the §1.7 checker enforcing both halves on new revisions; a DB-exercising readiness probe so `/health` cannot stay green through a database outage | desk §5a, discipline §1.7 |
| **Deploy freshness** (new, from the entry gate) — prod ran 3.5 days behind `main` through the entire Vegas window, and "the chunking fixes shipped" was true of `main` and false of production — **PARTLY SHIPPED**: `scripts/check_deploy_freshness.py` + a daily workflow now measure and report the gap (app machines vs `origin/main`, via the `GH_SHA` image label). Auto-deploy on merge remains an open owner decision — the alarm observes, it does not deploy | #669 |
| Ingestion-side identity guard: suffix/diacritic/variant-aware matching **before** a new `players_master` row is created — the Jr./II/accent dup class has been re-fixed downstream at least three times — **SHIPPED** (#693 via PR #696): variant-aware matching in the resolution path, with ambiguous variants left reviewable rather than resolved into competing identities (the father/son namesake trap) | backlog 4.3 |
| Import contract 4 (`event_desk` ↛ SL) with its `ignore_imports` baseline — lands here so the list shrinks across Phases 1 and 5 rather than being written after the decoupling — **SHIPPED**: baseline is **4 entries across 3 of 9 modules**, not the "fails broadly" wall the spec predicted; Phase 5 starts from a much shorter list | discipline §3.1 |

**Note:** the version-flip lands here, not in Phase 3, because it satisfies P2 and creates the
time axis Phase 3 consumes. It was previously described as "*the* transaction fix" — the entry
gate retired that framing: the transaction that caused #669 was `_run_venue`'s, already chunked.

**Already true, now verification rather than build work:** bounded Desk lock waits ship in the
deployed image as of Jul 23 01:52 UTC. Confirm it in a live window; do not rebuild it.

**Exit:** hourly tick completion measured **inside live-game windows** (not daily-averaged) meets
target across several natural overlapping cycles; no transaction holds a lock across network I/O;
a rebuild is safely re-runnable; **a deploy cannot block public reads** (migration lock-safety
verified against a staging reproduction) and **production is not silently behind `main`**.

---

## Phase 2 — Stat engine consolidation

**Why third:** best value/risk ratio in the program, and it gates both Phase 3's read-switch and
Phase 5's decomposition. Behavior-preserving.

**Part of this phase already landed early.** The spec's phasing step 5 includes "remove the
full-wipe (coordinates with doc #3's transaction work)" — that shipped in Phase 1 as the
version-flip (#697), so the metrics tables are already dated and versioned and the unscoped
deletes are gone. What step 5 still owes is the Explorer read-switch, which is Phase 3.

| Item | Spec |
|---|---|
| Golden-number parity harness (offline == live == leaderboard) **before** deleting any copy | stat-engine §4 |
| **In-event metrics scoping** (#701) — **moved here from Phase 1.** `compute()` loads and fits the full pool on every rebuild; #694's gate only skips when inputs are unchanged, so in a live event the full cost runs hourly. The promising direction is separating the slow-changing league-wide fit from the per-tick projection, which is engine surgery: it splits `compute()` and `ComputeResult`. **Sequence it after the parity harness** — its acceptance is "values identical to a full recompute", which has no mechanism until that harness exists | stat-engine §4, #669 |
| Lift the engine to `app/services/stats/` (contract 3 already guarding it) | stat-engine §1 |
| Collapse the ~8 TS%/eFG%/TOV%/GmSc sites and the duplicated per-36/per-100 scaling | backlog 1.1, 1.3 |
| Unify the two `_percentile` implementations | backlog 1.2 |
| Metric registry (one formula, with `registry_version`) + capability model | stat-engine §2–3 |
| Stat-constant confinement rule (`0.44` only under `app/services/stats/`) — lands *with* the cleanup so it protects it | discipline §1.3 |

**Exit:** parity tests green in CI; every duplicate formula site deleted; a formula change requires
editing exactly one place.

---

## Phase 3 — Longitudinal payoff *(low risk, as intended)*

**Why now low-risk:** the plumbing shipped in Phase 1 and the math was unified in Phase 2, so this
is mostly a read-path switch plus a product surface.

| Item | Spec |
|---|---|
| Switch the Explorer's **default full-competition view** from live recompute to reading `is_current` dated snapshots — safe only now that formulas are unified. Sub-season and recombinable grains (per-game, last-N, date filters) cannot be served from a season-grain snapshot and keep calling the shared engine live | stat-engine §5 |
| Three version stamps + as-of date on the metrics tables (`environment_profiles` conventions, not `MetricSnapshot`) | stat-engine §5 |
| Within-event daily trend surface (GmSc / TS% / BPM across an event) | stat-engine §6 |

**Exit:** a player's advanced line is queryable as a time series; the Explorer's default-grain
values match stored values by construction, and sub-season grains match the engine via the parity
harness; the trend surface renders from retained history.

---

## Phase 4 — Journey-graph conversion

**Why here:** the largest architecture-shaping phase, now protected by Phase 0's contracts and
informed by a stabilized stat/ops layer.

| Item | Spec |
|---|---|
| Domain vocabulary — `temporal.py` first (`Watermark`, `VersionStamps`, `Scope`), then identity/spoke | vocabulary doc |
| Light namespacing: class/module/docstring alignment, no `__tablename__` changes | alignment §5a |
| Adopt `DatedVersionMixin` (defined in Phase 1) on the remaining versioned tables | alignment §5b |
| **Organization → team/program model + affiliation retarget** — the single blocker for spoke #2 | alignment §3 |
| Service reorganization into `stats/` `backbone/` `ingest/` `sources/<spoke>/` | alignment §4 |
| Canon-entity promotion (edition / game / provenance) — best done *alongside* spoke #2 | alignment §5, Wave C |

**Exit:** `team_program_id` populated and affiliations retargeted; a non-NBA source could assert an
affiliation; the backbone doc's code-location table has no stale rows.

---

## Phase 5 — Structural cleanup

**Why last:** this tidies work that must exist first, and the god-file seams are only unambiguous
once the freshness layering and the stat math are settled. Its guardrails already landed in
Phases 0–1 and have been constraining the work throughout.

| Item | Spec |
|---|---|
| God-file decomposition along now-unambiguous seams — the file-size rule's net-change evaluation permits this by design | backlog 5.2, desk §6 |
| DTO standardization into `app/models/` / `app/domain/` | backlog 5.3 |
| Desk freshness contract + layer collapse (removes request-time splicing) | desk §1, §3 |
| Resolve the `roster_status` dual-write | backlog 4.1 |

**Exit:** every user-visible Desk assertion carries one watermark; no request-time field splicing
remains; the contract-4 ignore list is strictly shorter than at Phase 1, and each remaining entry
is a known, named coupling.

**Accepted coupling (decision, not a gap):** contract 4 only forbids `event_desk` importing
Summer League. The reverse direction — Summer League importing `event_desk` — is real and
pervasive today: `metrics_rebuild_gate.py`, `desk_read.py`, `desk_tick/*`, `event_window.py`, and
`app/cli/summer_league_ingest_runner.py` all import from `app.services.event_desk` (registry,
lifecycle, timeutils, render_snapshots, snapshot_materialization, state_machine, controller). This
is not an oversight uncovered by review — it is how the version-flip publish (#697) and the Desk
tick's latency-class split (#699) actually share the render-snapshot/lifecycle machinery — but it
is uncovered by any import-linter contract, which makes it invisible drift risk. Adding a forbidden
contract for this direction now would start with a double-digit baseline (the mirror image of
contract 4's original prediction), not the small ratchet contract 4 turned out to be, and Summer
League is the *only* concrete `event_desk` consumer at N=1 — the same "framework-shaped at N=1 is
the trap" reasoning that defers Event Desk generalization applies here. **Decision: accepted as
Phase 5 material, not contracted now.** Phase 5's "layer collapse" item is where this gets resolved
for real, by moving the shared render-snapshot/lifecycle logic to wherever it stops mattering which
direction the import runs; a contract before that move would just be pre-baselining debt this phase
intends to delete outright. Re-open this decision if a second event/spoke needs `event_desk` before
Phase 5 lands — that is the condition under which the coupling stops being one-way in practice and
a contract earns its keep.

---

## Open tickets mapped to this plan

The live issue queue and this plan describe the same work; keep them pointing at each other.

| Ticket(s) | Relationship |
|---|---|
| **#669** (incident record) | The observed evidence behind Phase 1's entry gate, and the source of the migration-safety items. **Entry gate closed** — the diagnosis is a comment on the issue; migration safety shipped as the phase's first change. Remaining open follow-ups on the issue (metrics-compute scoping is only half closed — see #701): a maximum transaction/writer-lock lifetime per cron phase, alerting on old transactions and pool saturation, and per-stage cron telemetry that survives machine restarts |
| **#697, #698, #699** (Phase 1 remainder) | **Closed** (PRs #702, #703, #706): version-flip publish, intra-day compaction, and latency-class partitioning all shipped. Phase 1's build work is complete; only the live-window exit criteria remain |
| **#701** (in-event metrics scoping) | **Phase 2**, re-filed from Phase 1, still open. Its precondition is Phase 2's golden-number harness, and its fix restructures the engine Phase 2 lifts — it is inherited by that engine's class machinery rather than fixed standalone. Phase 1 contains the cost via #699 (shipped) rather than reducing it |
| **#692, #693, #694** | **Closed** (PR #696): runtime network/writer-lock guards, the ingestion identity guard, and the metrics rebuild gate. #694 stopped the *unchanged-input* recompute only; the in-event full-pool cost it was meant to bound is still open as #701 |
| **#661** (scheduler success ≠ data refresh) | Implements the operational half of desk §1's watermark contract — scheduler / source / projection / snapshot signals kept distinct. Complements Phase 5's user-facing contract; ship whenever ready |
| **#662–#667** (Desk decomposition) | Phase 5 material. **Sequencing guard:** execute after Phase 2's stat consolidation, or restrict each ticket to pure moves — otherwise duplicated math gets scattered into more files (sequencing rule 3) |
| **#645, #646, #648** (Explorer/environment modularization) | Same Phase 5 guard applies |
| **#675** (merge FK classification) | **Closed.** The live half of Phase 0's merge-coverage row: the guard enumerated 13 unclassified FKs, and all 13 are now registered for reassignment with the baseline empty. Closes the long-standing "merge omits `summer_league_*` tables" bug |
| **#655** (xdist vs. remote Neon test DB) | Test-infrastructure prerequisite for trustworthy phase exits; independent of any phase, worth doing early |
| **#626–#630, #632** (shipped) | The "already fixed at HEAD — do not re-fight" record |

## Deferred by decision

- **Event Desk framework generalization** — hold until a real second event forces the seams
  (desk §7). Framework-shaped at N=1 is the trap this whole retrospective diagnosed.
- **Staleness thresholds** — deferred; Phase 5's freshness work will need them (desk, open Qs).
- **Cross-stage Player Development Ledger** — the larger initiative the Phase 3 substrate enables.
- **Level-adjusted metric translation model** — still open; Phase 2 supplies the engine it lives
  in, not the translation study.

## Standing caveats

- **The transaction diagnosis is now observed, not just read from code.** Incident #669 confirmed
  the long-ingestion-transaction/writer-lock chain live in production; Phase 1's entry gate
  narrows *which path* held it before converting.
- **Nothing here has been proven by building it.** These specs are grounded in code verified at
  HEAD, but the phases are estimates of sequence, not of effort.
- **Phase boundaries are for sequencing, not for merging.** Each phase contains multiple
  independently shippable changes; do not batch a phase into one large merge — that shape is
  precisely what the failure record indicts.
