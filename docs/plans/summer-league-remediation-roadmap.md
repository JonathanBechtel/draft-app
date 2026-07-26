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

**Entry gate — close the remaining diagnosis before building.** Incident **#669** (July 22–23)
supplies the observed evidence the first draft of this plan lacked, and corrects an attribution:
a ~96-minute **full-ingestion** transaction held the writer lock (the "96-minute Desk
transaction" was actually 96 minutes of the Desk *waiting*, after which it resolved dormant and
exited); the deploy's non-concurrent `CREATE INDEX` migration queued behind that transaction;
and public routes 500ed on web-pool exhaustion while `/health` stayed green. (The record's
lock-chain reading for *reads* is imprecise — a bare `CREATE INDEX` requests a `SHARE` lock,
which blocks writes, not reads — so the exact read-blocking mechanism is one more thing this
gate confirms; see desk §5a.) The long-transaction/lock class is therefore
**confirmed live** — Phase 1's position is no longer provisional. What the entry gate still
resolves is *which path* to convert first: whether the deployed image was missing the shipped
chunking fixes, and whether the holder was the surviving mega-transaction — noting #669's
finding that the "scoped" rebuild limits which rows are *persisted* while `compute()` still
loads and recalculates the full historical dataset, so scoping does not bound transaction
length.

| Item | Spec |
|---|---|
| Runtime guards: no network I/O while a transaction is open or the writer lock is held (ContextVar + client checks; hard-fail dev/test, warn+stack in prod) | discipline §2.1–2.2 |
| `DatedVersionMixin` (`version` / `registry_version` / `calculation_version` / `is_current` / `as_of`) — defined **here** so the version-flip tables inherit it from day one | alignment §5b |
| **Version-flip publish replacing the mega-transaction** — build the new metrics version outside any lock, materialize variants, flip the pointer in a tiny transaction (`app/cli/summer_league_ingest_runner.py:1308-1348`) | desk §5, stat-engine §5 |
| Intra-day compaction policy for hourly rebuild versions (retain daily close + current) — lands with the version-flip so retention is bounded from the first event | stat-engine §5 |
| Stop deleting `SummerLeagueMetricModel` (auditable fit history) | stat-engine §5 |
| Latency-class partitioning: fast live poller / medium projection builder / slow backbone | desk §2 |
| **Migration safety** (from #669): short `lock_timeout` in release migrations; `CREATE INDEX CONCURRENTLY` (via `autocommit_block()` — required by this repo's Alembic setup) on large tables; a DB-exercising health signal so `/health` cannot stay green through a database outage | desk §5a, discipline §1.7 |
| Ingestion-side identity guard: suffix/diacritic/variant-aware matching **before** a new `players_master` row is created — the Jr./II/accent dup class has been re-fixed downstream at least three times | backlog 4.3 |
| Import contract 4 (`event_desk` ↛ SL) with its `ignore_imports` baseline — lands here so the list shrinks across Phases 1 and 5 rather than being written after the decoupling | discipline §3.1 |

**Note:** the version-flip lands here, not in Phase 3, because it *is* the transaction fix. It
simultaneously satisfies P2 and creates the time axis Phase 3 consumes.

**Exit:** hourly tick completion measured **inside live-game windows** (not daily-averaged) meets
target across several natural overlapping cycles; no transaction holds a lock across network I/O;
a rebuild is safely re-runnable.

---

## Phase 2 — Stat engine consolidation

**Why third:** best value/risk ratio in the program, and it gates both Phase 3's read-switch and
Phase 5's decomposition. Behavior-preserving.

| Item | Spec |
|---|---|
| Golden-number parity harness (offline == live == leaderboard) **before** deleting any copy | stat-engine §4 |
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

---

## Open tickets mapped to this plan

The live issue queue and this plan describe the same work; keep them pointing at each other.

| Ticket(s) | Relationship |
|---|---|
| **#669** (incident record) | The observed evidence behind Phase 1's entry gate, and the source of the migration-safety items |
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
