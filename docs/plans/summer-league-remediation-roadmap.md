# Summer League Remediation Roadmap

**Status:** Sequencing plan. Converts the retrospective doc set into ordered, shippable phases.

**Reads with:** `north-star-architecture.md` (principles and doc map). Each phase below points at
the spec that owns its detail — this doc is the order, not the design.

## Sequencing rules

Three rules produced this order. They are worth stating because they explain the non-obvious
placements.

1. **Guardrails are installed just-in-time, ahead of the work they protect — not as a phase.**
   Import contracts for packages that do not yet exist are free and permanent; the same contracts
   written *after* the package is built are retrofits against code that already drifted.
2. **Plumbing and payoff separate.** Where one change both fixes an operational fault and unlocks
   a product capability, the change lands in the operational phase and the capability follows.
   This avoids fixing the same thing twice.
3. **Unify computation before relocating it.** Deduplicating the stat math precedes both reading
   materialized values and splitting god files — otherwise duplication gets frozen into tables or
   scattered into more files.

Every phase carries **exit criteria**. The failure record's decisive process defect was
operational acceptance deferred past merge; a phase is not done because its code merged.

---

## Phase 0 — Free guardrails + no-op simplification

**Why first:** zero risk, clears noise, and installs the protections everything downstream needs.

| Item | Spec |
|---|---|
| Import contracts 2, 3, 5 (`app.domain`, `app.services.stats`, spoke independence) — written *before* those packages exist, so they start green | discipline §3.1 |
| Unscoped-`delete()` AST checker — the direct P2 guard | discipline §1.1 |
| Double-uPER compute fix (`metrics.py:728-732`) | backlog 1.4 |
| Remove dead Explorer branches (`available=False` teams/games subjects) | backlog 5.1 |
| Naming/layout normalization; document the backfill ordering precondition | backlog 5.4, 6.3 |

**Exit:** contracts run in CI and pass; the delete checker fails on a deliberately introduced
violation; no behavior change observable in the app.

---

## Phase 1 — Operational: cron and database reliability

**Why second:** this is the live pain, and the most user-visible failure — updates that did not
land while games were in progress.

| Item | Spec |
|---|---|
| Runtime guards: no network I/O while a transaction is open or the writer lock is held (ContextVar + client checks; hard-fail dev/test, warn+stack in prod) | discipline §2.1–2.2 |
| **Version-flip publish replacing the mega-transaction** — build the new metrics version outside any lock, materialize variants, flip the pointer in a tiny transaction (`ingest_runner.py:1308-1348`) | desk §5, stat-engine §5 |
| Stop deleting `SummerLeagueMetricModel` (auditable fit history) | stat-engine §5 |
| Latency-class partitioning: fast live poller / medium projection builder / slow backbone | desk §2 |
| Confirm the production timeout cause — mega-transaction vs. undeployed fixes (**still undiagnosed**) | desk §5 |

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
| Switch the Explorer from live recompute to reading `is_current` dated snapshots — safe only now that formulas are unified | stat-engine §5 |
| Three version stamps + as-of date on the metrics tables (`environment_profiles` conventions, not `MetricSnapshot`) | stat-engine §5 |
| Within-event daily trend surface (GmSc / TS% / BPM across an event) | stat-engine §6 |

**Exit:** a player's advanced line is queryable as a time series; Explorer values match stored
values by construction; the trend surface renders from retained history.

---

## Phase 4 — Journey-graph conversion

**Why here:** the largest architecture-shaping phase, now protected by Phase 0's contracts and
informed by a stabilized stat/ops layer.

| Item | Spec |
|---|---|
| Domain vocabulary — `temporal.py` first (`Watermark`, `VersionStamps`, `Scope`), then identity/spoke | vocabulary doc |
| Light namespacing: class/module/docstring alignment, no `__tablename__` changes | alignment §5a |
| `DatedVersionMixin` — P2 encoded as a type | alignment §5b |
| **Organization → team/program model + affiliation retarget** — the single blocker for spoke #2 | alignment §3 |
| Service reorganization into `stats/` `backbone/` `ingest/` `sources/<spoke>/` | alignment §4 |
| Canon-entity promotion (edition / game / provenance) — best done *alongside* spoke #2 | alignment §5, Wave C |

**Exit:** `team_program_id` populated and affiliations retargeted; a non-NBA source could assert an
affiliation; the backbone doc's code-location table has no stale rows.

---

## Phase 5 — Remaining guardrails + structural cleanup

**Why last:** these protect and tidy work that must exist first.

| Item | Spec |
|---|---|
| Diff-scoped file-size rule (pre-commit warns, CI enforces; net-change evaluation so it cannot block decomposition) | discipline §1.4 |
| Ruff complexity rules (`C901`, `PLR0915`, `PLR0913`) with `per-file-ignores` baseline | discipline §1.6 |
| Import contract 4 (`event_desk` ↛ SL) with `ignore_imports` baseline — the shrink list becomes the decoupling progress meter | discipline §3.1 |
| God-file decomposition along now-unambiguous seams | backlog 5.2, desk §6 |
| DTO standardization into `app/models/` / `app/domain/` | backlog 5.3 |
| Desk freshness contract + layer collapse (removes request-time splicing) | desk §1, §3 |
| Resolve the `roster_status` dual-write | backlog 4.1 |

**Exit:** no file over threshold grew; every user-visible Desk assertion carries one watermark;
the contract-4 ignore list is strictly shorter than at Phase 5 start.

---

## Deferred by decision

- **Event Desk framework generalization** — hold until a real second event forces the seams
  (desk §7). Framework-shaped at N=1 is the trap this whole retrospective diagnosed.
- **Staleness thresholds** — deferred; Phase 5's freshness work will need them (desk, open Qs).
- **Cross-stage Player Development Ledger** — the larger initiative the Phase 3 substrate enables.
- **Level-adjusted metric translation model** — still open; Phase 2 supplies the engine it lives
  in, not the translation study.

## Standing caveats

- **The production timeout is diagnosed from code, not observed behavior.** Phase 1 should confirm
  the cause before assuming the mega-transaction is it.
- **Nothing here has been proven by building it.** These specs are grounded in code verified at
  HEAD, but the phases are estimates of sequence, not of effort.
- **Phase boundaries are for sequencing, not for merging.** Each phase contains multiple
  independently shippable changes; do not batch a phase into one large merge — that shape is
  precisely what the failure record indicts.
