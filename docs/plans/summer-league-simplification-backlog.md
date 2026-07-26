# Summer League — Simplification & Redundancy-Removal Backlog

**Status:** Working plan. Refactoring/strategy document — no code changes proposed inline.

**Companion to:** `docs/summer-league-desk-history.md` (the failure record). That doc is
*historical*; this one is *forward-looking* and organized by redundancy, not by incident.

**Scope of this doc (doc #1 of a five-part set):** the safe-to-do-now simplifications and
duplication removals that make the Summer League systems "reproduce themselves easier" and
stop the same logic from living in many places and drifting apart. The behavior-changing
work it uncovers is *named here but graduated* to the specs that own it:

| # | Doc | Owns |
|---|-----|------|
| 1 | **this doc** | simplification & redundancy removal, sequencing |
| 2 | stat-engine reuse spec | one source-agnostic stat engine + capability model |
| 3 | Desk simplification spec | freshness contract, latency-class decoupling |
| 4 | backbone generalization spec | affiliation org model, source-as-adapter, dual-write |
| 5 | north-star architecture | canonical-record + projections principle |

## How to read this backlog

Every item is a **redundancy or self-conflict**, not a file. Each carries:

- **Evidence** — concrete `path:line` sites (verified against HEAD `c78af8d`).
- **Collapse to** — the single form it should become.
- **Class** — one of:
  - 🟢 **SAFE HYGIENE** — internal-only; behavior-preserving; do anytime behind tests.
  - 🟡 **BEHAVIOR-CHANGING** — alters live operational behavior; must graduate to spec #3/#4
    and get its own verification, not be smuggled into a "cleanup" PR.
- **Prereq / sequence** — what must land first.

## Guiding principles

> **Canonical home: `north-star-architecture.md`.** That doc is authoritative for P1/P2 and adds
> P3 (sources are adapters) and P4 (freshness means source currency). Restated here only so this
> backlog reads standalone — amend them there, not here.

**P1 — One canonical record; projections are thin readers.**

> Keep one durable canonical record. Everything users see is a thin, disposable projection
> computed *from* that record through *one* code path, each carrying an explicit watermark.

Every redundancy below is a violation of P1 in one of two directions: the same computation done
N times (stat math, percentiles), or the same fact stored N times and allowed to drift (roster
status, freshness clocks, offline-vs-live stat columns).

**P2 — Longitudinal-first: retain history by default; destructive rebuild is the exception.**

> Anything that carries analytical or evidentiary value is materialized **append-only and
> as-of-dated**, with an atomic current-version pointer. History is never overwritten. The
> "wipe clean and recompute" rebuild is an **anti-pattern**, not a shortcut — it destroys the
> product's most valuable asset (the time axis) on every run.

This is a default posture for *most data work*, not a Summer League detail. The actionable line
— **evidence vs. cache** — keeps it from becoming dogma:

| Data kind | Example | Rule |
|---|---|---|
| **Canonical facts (assertions)** | game logs, affiliations, participation | Append-only, bitemporal; **never** destroyed. |
| **Time-varying analytical projections** | player advanced lines, cohort baselines, environment profiles | **Dated version-flip; history retained.** ← the band the full-wipe violates. |
| **Pure regenerable presentation caches** | render-snapshot variants | Overwrite-in-place OK (no independent value), but must stamp the **watermark of the projection they render**. |

**Good news — the anti-pattern is narrowly concentrated, not systemic** (full inventory in
Appendix A). An app-wide audit found the SL metrics rebuild is essentially the *only* analytics
offender. The rest of the app is already longitudinal-first, and in fact carries a **mature,
reusable versioning primitive** the SL rebuild simply never adopted: offline percentiles/z-scores
(`compute_metrics.py`), combine scores, and KNN comps all write versioned `MetricSnapshot` +
`player_metric_values` rows (`run_key`/`version`/`is_current`); consensus writes append-only
snapshots; `environment_profiles` and `cohort_baselines` version-flip.

So P2 **promotes an existing, proven pattern to the default** — it does not impose a new one. It
demotes the destructive `rebuild_sl_metrics` full-wipe (`metrics.py:1443-1446`) to a rare,
justified exception. Issue B (1.6) is the first application; the same rule governs every future
spoke's materialization.

## Current-state note — battles already won (do NOT re-fight)

The postmortem predates these. Confirmed present at HEAD; treat as **done**, build on them:

- **Gemini/embedding calls no longer run inside the writer lock.** Identity resolution is
  split into a lock-free *preparation* pass (all Gemini calls) and small locked write batches
  (`RESOLUTION_BATCH_SIZE = 8`). See `player_resolution.py:719-723`,
  `app/cli/summer_league_ingest_runner.py:656-728`.
- **The 87-minute whole-venue transaction is chunked.** Shot/PBP normalization runs
  `EVENT_BATCH_SIZE = 8` games per `db.begin()`, releasing the lock between batches
  (`app/cli/summer_league_ingest_runner.py:548-653`).
- **The Desk lock wait is bounded to 30s** (`sl_desk_tick.py:267,1005-1009`) — the Desk can
  no longer be starved for an hour; it times out and retries.
- **The "no-op advances freshness" badge bug is cut.** The dormant/off-window path returns
  early without invoking the controller or materializing snapshots
  (`sl_desk_tick.py:1061-1086`); `content_refreshed_at`/`next_tick_eta` are gated on
  `content_updated` (`controller.py:67-78`).

---

# Bucket 1 — Duplicated stat math → one engine (highest leverage)

The reusable engine **already exists**: `app/services/summer_league/metrics.py` computes every
metric off a neutral `Box` dataclass (`metrics.py:200-220`) and `LeagueContext`
(`metrics.py:245-264`) — fully source-decoupled. The problem is that its formulas are
re-implemented from scratch at request time in four other places. This is the single most
valuable cleanup and the foundation of doc #2.

**This bucket is two independent tickets — keep them separate:**

- **Issue A — dedupe-and-lift** (1.1–1.4 below). Consolidate the duplicated math; point every
  surface at the one engine. Behavior-preserving, guarded by golden-number tests. Wave 1.
- **Issue B — materialize-and-read, dated** (1.6 below). Stop recomputing live; read
  precalculated values off the table. Separate risk profile; **depends on Issue A** (don't
  freeze duplicated math into a table). Reframed by the longitudinal requirement — see 1.6.

### 1.1 — Box-derived ratio formulas implemented 3–8× each 🟢→🟡

- **Evidence.** TS% denominator `2·(FGA + 0.44·FTA)` appears at **≥8 sites**: canonical
  `metrics.py:650`; Explorer Python `explorer_service.py:1365,2568`; Explorer SQL strings
  `2663,2731,3321,3560`; Explorer filter/sort `1619,1691,1739`; metrics-service
  `summer_league_metrics_service.py:192-203,718`. eFG%, TOV%, 3PAr, FTr, Game Score follow the
  same pattern (Game Score is re-derived in SQL at `explorer_service.py:2610-2624` with a
  comment: *"Mirrors `game_score` exactly"*).
- **Risk.** Changing one coefficient (e.g. the 0.44) requires editing ~8 sites or the offline
  column silently disagrees with the live Explorer cell — the stats-domain twin of the Desk's
  prose-vs-metric contradiction.
- **Collapse to.** One canonical formula per metric in `metrics.py`; every consumer calls it.
  Where the Explorer needs SQL for push-down filtering/sorting, generate the SQL expression
  from a single formula definition rather than hand-writing it a fourth time (see doc #2's
  metric-registry design — a formula declared once, emitted as both Python and SQL).
- **Class.** 🟢 for the Python recompute sites (pure behavior-preserving swap). 🟡 where SQL
  push-down changes query shape — guard with golden-number tests asserting *offline column ==
  live Explorer value == leaderboard value* for a fixed fixture before and after.
- **Prereq.** None to start the Python sites. The SQL-generation half is doc #2 proper.

### 1.2 — Two copies of the percentile/baseline normalizer 🟢

- **Evidence.** Linear-interpolated `_percentile` is copy-duplicated at
  `summer_league_environment_service.py:764-776` and `summer_league/cohort_baselines.py:243-283`;
  a third reverse-lookup variant `percentile_of_value` lives at `desk_grades.py:123`. No
  z-score/stdev normalization exists anywhere (normalization is entirely percentile-based).
- **Collapse to.** One `percentiles` utility (forward + reverse lookup) that all three
  subsystems import. They're already "consistent by comment" — make them consistent by code.
- **Class.** 🟢 pure utility extraction.

### 1.3 — Per-mode scaling (per-36 / per-100) duplicated Python+SQL 🟢

- **Evidence.** `_compute_player_values` (`explorer_service.py:2527-2541`, Python) and
  `_scaled_sort_expr` (`2591-2607`, SQL) implement the same scaling twice — the SQL version's
  comment says *"Mirrors the arithmetic in `_compute_player_values`."* pts_per100 re-derived
  again in `rollup_recombinable` (`1389-1409`).
- **Collapse to.** One scaling definition consumed by both display and sort paths.
- **Class.** 🟢.

### 1.4 — Micro-bug: uPER computed twice identically 🟢 ✅ Done in Phase 0

- **Evidence.** `metrics.py:728-732` calls `compute_uper(...)` and immediately recomputes the
  identical value on the next lines. Harmless but wasteful and confusing.
- **Collapse to.** Compute once. Trivial.
- **Class.** 🟢.
- **Resolved.** Second compute+assign pair removed. `compute_uper` is pure, so the removal is
  provably value-preserving.

### 1.5 — (Net-new, not a dedupe) capability map: `metric → required inputs`

- **Gap.** There is no `metric → required inputs → source provides` mapping. Availability is
  expressed today as coarse per-pool booleans (`pbp_available`, `shotchart_available`,
  `adv_eligible`, `data_quality` in `normalization.py:699-717,1553-1588`) plus scattered inline
  `None`-when-missing checks (e.g. `astd_pct` gated ad hoc at `explorer_service.py:2581-2588`).
- **This is the one genuinely new piece** — it belongs to doc #2 and is what lets the engine
  answer "given this source's inputs, which metrics are computable" mechanically instead of by
  hard-coded assumption. Listed here so the dedupe work in 1.1–1.3 is designed to hang off it.

### 1.6 — Issue B: materialize-and-read as **dated snapshots** (unlocks longitudinal) 🟡

This is not really "store vs. recompute." It is "materialize as *as-of-dated* snapshots and
read the latest" — where *read the latest* serves the Explorer and *read the series* serves a
longitudinal view. One design change resolves an operational fire, a reproducibility gap, and a
missing product capability at once.

- **Current state.** SL player metrics **are** already materialized — `summer_league_player_seasons`,
  one row per (player, competition), with `created_at`/`updated_at` but **no as-of date axis**
  (`app/schemas/summer_league_metrics.py:105-235`). The Explorer nonetheless **ignores** that
  table and recomputes live from box components (`explorer_service.py:2564-2577`). And
  `rebuild_sl_metrics` **full-wipes** the table every run (`metrics.py:1443-1446`
  `delete(SummerLeaguePlayerSeason)`). So history is destroyed on every rebuild.
- **The convergence.** That full-wipe is the *same object* as three logged problems:
  1. the live operational fire (the heavy unscoped wipe inside the mega-transaction, Bucket 3);
  2. the reproducibility gap (destructive rebuild vs. clean versioned one, Bucket 6.1);
  3. the reason no longitudinal view can exist (history overwritten each run).
- **Collapse to.** Apply the **append-only, version-flip** pattern **already used in this
  codebase** by `environment_profiles` and `cohort_baselines` (write a fresh dated version, flip
  `is_current` atomically, never mutate priors) to the player-metrics table. This simultaneously
  (a) removes the destructive wipe from the hot transaction, (b) makes rebuilds safe to re-run,
  and (c) yields a daily time series of every player's advanced line for free.
- **Two longitudinal grains it unlocks (scope honestly):**
  - **Within-event daily trend** — near-term, cheap: how a player's cumulative GmSc / TS% / BPM
    moved across the ~2-week tournament. Falls out of dated materialization almost for free.
  - **Cross-stage career ledger** — the big initiative, already designed in
    `docs/plans/player-longitudinal-evidence-layer-pitch.md` (Player Development Ledger, with
    metric-family comparison semantics and minimum-sample rules). **Extend that, don't reinvent
    it**; the within-event daily snapshots become one well-formed stage in that ledger.
- **Class.** 🟡 — schema + rebuild-path change; graduates to doc #2 (engine) and coordinates
  with doc #4 (backbone/journey-graph). Depends on Issue A.

---

# Bucket 2 — Competing "current" mechanisms → one watermark contract 🟡

Graduates to **doc #3**. Listed here because it's a redundancy (six clocks for one card), but
it is behavior-changing and must not be done as casual cleanup.

- **Evidence — six timestamp families still coexist at HEAD:**
  1. `lifecycle_observed_at` — always `now` (`controller.py:57`).
  2. `content_refreshed_at` + `next_tick_eta` — the user-facing "as of", gated on
     `content_updated` (`controller.py:67-78`; read at `desk_read.py:540-541`).
  3. `source_freshness_tick_at` / `_next_tick_eta` — copied onto each render snapshot
     (`event_desk_render_snapshot.py:119-123`; re-judged vs request `now` at `desk_read.py:516`).
  4. Request-time live-overlay `now` (`desk_read.py:1135-1240`).
  5. Six `summer_league_pipeline_states` process columns (`pipeline_state.py:110-149`).
  6. The historical dormant-tick clock — **already cut** (see current-state note).
- **Collapse to.** Two authoritative watermarks — a **source-data watermark** and a
  **projection watermark** — that every displayed number and sentence derives from, so
  "fresh" means exactly one thing. The process/scheduler columns (family 5) are fine to keep
  as *operational telemetry* but must be visibly distinct from user-facing freshness.
- **Retire specifically:** the request-time single-line prose rewrite at
  `desk_read.py:1171-1184` — it patches *one sentence* to match a live value while the rest of
  the projection stays tick-aged. Replace with re-deriving the projection, not splicing a line.
- **Class.** 🟡 → doc #3.

---

# Bucket 3 — Fast/slow entanglement → the remaining hotspot 🟡

Graduates to **doc #3**. Most of this bucket is already fixed (see current-state note). One
large critical section remains, and the failure class was **confirmed live by incident #669**
(July 22–23): a ~96-minute full-ingestion transaction holding the writer lock, the Desk starved
behind it, and a deploy migration queueing public reads into a 500 outage (see desk §5a).

- **Evidence — remaining mega-transaction.** `app/cli/summer_league_ingest_runner.py:1308-1348` wraps,
  inside **one** `db.begin()` under the writer lock:
  `rebuild_sl_metrics(db)` (a *full unscoped table wipe* — `metrics.py:1443-1446` deletes all of
  `SummerLeaguePlayerSeason` / `MetricContext` / `MetricModel`) → `materialize_desk_render_snapshots`
  (72 variants) → `refresh_environment_profiles_for_year`. The lock is held for the entire
  rebuild + materialization + environment refresh.
- **Why it's coupled by design.** `app/services/event_desk/snapshot_materialization.py:1-7`
  states the coupling in its module docstring: *"Every
  full metrics rebuild must therefore replace the render-snapshot matrix before its transaction
  commits, or direct stat surfaces and the homepage can disagree."* That is fast/slow coupling
  baked into an invariant.
- **Collapse to.** Scope the rebuild (don't full-wipe on the hourly path — the tick docstring at
  `sl_desk_tick.py:39-43` already warns this script is "far too heavy for an hourly cron"); and
  break the "rebuild must materialize before commit" coupling via a versioned/atomic-swap
  publish so stat surfaces and homepage stay consistent *without* one giant lock hold.
- **Action item (this session):** confirm whether the timeouts you saw yesterday are (a) this
  mega-transaction or (b) the already-shipped fixes not being deployed. That determines whether
  doc #3 leads with a code change or a deploy verification.
- **Class.** 🟡 → doc #3.

---

# Bucket 4 — Duplicated state (drift risk) 🟡

- **4.1 — `roster_status` dual-write.** `SummerLeagueParticipation.roster_status` denormalizes
  what `player_affiliations` already asserts ("fast read" copy). Two writers, one truth — the
  data-layer version of the multiple-clocks disease. **Collapse to:** single source of truth in
  `player_affiliations`; derive the fast read or drop the copy. 🟡 → doc #4.
- **4.2 — Offline stat columns vs. live recompute.** `summer_league_player_seasons` stores
  `ts_pct/efg_pct/…` (`metrics.py:1332-1360`) that the Explorer ignores and recomputes from box
  components at request time (`explorer_service.py:2564-2577`). Resolving Bucket 1.1 (one formula)
  makes the stored column and the recompute provably equal. The *decision* — store-and-read vs.
  always-recompute — is answered by **Issue B / 1.6**: materialize as dated snapshots and read
  the latest, which the longitudinal requirement makes the clear choice. 🟡 → doc #2.
- **4.3 — Duplicate `players_master` rows (the recurring identity-dup class).** Suffix, diacritic,
  and variant forms (Carter vs. Carter Jr., Salaün vs. Salaun) plus first-initial merge collisions
  have created second canonical rows repeatedly: behind the #495 "missing star players"
  investigation (dup rows fragmenting identity, not missing data), the lottery-filter pollution
  (Gary Payton II inheriting his father's draft record), and flagged again in the Explorer
  position-filter work. Every fix was downstream — an audit script, a fix script, a manual merge
  pass — and **no ingestion-side guard stops the next variant from creating a second row.** Two
  rows for one person is the identity-layer form of duplicated state, and identity is the moat.
  **Collapse to:** variant-aware matching (suffix/diacritic normalization) in the resolution path
  *before* row creation, plus a recurring dup audit. 🟡 → doc #4 (identity hub); roadmap Phase 1.
- **4.4 — `player_merge_service` child-table list drifts from the FK graph.** The manually
  maintained table list was never updated as `summer_league_*`, shot-event, and participation
  tables added FKs to `players_master`, so merging a player who holds SL data hard-fails on a
  RESTRICT FK. Nothing enforces that a new FK-bearing table gets registered — it will bite again
  on the next table. **Collapse to:** a test asserting every FK to `players_master` is
  *classified* — registered for reassignment or intentionally `ondelete="CASCADE"` (naive
  list == FK-graph equality would false-fail on the deliberate cascade exemptions, and
  auto-deriving the reassignment list would resurrect rows meant to die; see discipline §3.4).
  🟢 free test; roadmap Phase 0.
  ✅ **DONE in Phase 0.** The test (`tests/unit/test_player_merge_fk_coverage.py`) found 13 of
  36 FKs unclassified, and all 13 are now registered for reassignment — the pending list is
  empty. The constraint analysis made this far cheaper than estimated: only
  `summer_league_desk_player_grades` has a unique constraint containing the player column
  (`player_id, competition_id, baseline_version`), so it alone needs conflict columns; every
  other table keys on game/event ids and cannot collide. No migration and no cascade changes
  were needed. Proven by `tests/integration/test_player_merge_backbone_tables.py`, verified to
  fail with the original `ForeignKeyViolationError` when the registrations are removed. This
  also closes the "merge omits `summer_league_*` tables" bug.

---

# Bucket 5 — Dead weight & decomposition 🟢

All safe hygiene. **Do the god-file splits *after* Bucket 1**, or you'll just relocate
duplicated math into smaller files.

- **5.1 — Stubbed Explorer subjects.** ✅ **Done in Phase 0.** The premise was stale by the
  time it was actioned: teams and games had both shipped, and all seven `ExplorerResult`
  constructions passed `available=True`. What actually remained was the always-true flag, an
  unreachable "coming soon" template branch, its CSS rule, and a module docstring still
  describing the subjects as unimplemented. All removed.
- **5.2 — God-file decomposition seams.** Prime candidates by size:
  `summer_league_explorer_service.py` (198KB), `summer_league/desk_read.py` (109KB),
  `summer_league/normalization.py` (106KB), `summer_league_environment_service.py` (99KB),
  `summer_league_ingest_runner.py` (59KB), `metrics.py` (57KB), `desk_storylines.py` (54KB).
  Split along the natural seams (query-build vs compute vs render; ingest vs normalize vs
  metrics). **Prereq:** Bucket 1.
- **5.3 — DTO standardization.** Summer League has *no* `app/models/` presence; response DTOs
  are dataclasses scattered inside services (`GamesPage`, `ExplorerResult`, `EnvironmentScope`,
  `DeskTrackerSection`), unlike the rest of the app. Consolidate into `app/models/` for one place
  to find SL shapes.
- **5.4 — Naming/layout inconsistencies.** 🟡 **Partially done in Phase 0** — the internal
  half only, because Phase 0's exit criterion is *no behavior change observable in the app*.
  - ✅ Template dirs: `app/templates/summer_league/` renamed to `summer-league/`, so both SL
    template roots agree and match the kebab-case convention CLAUDE.md documents.
  - ⏸️ `static/` root vs `static/js|css/` placement (11 root assets): deferred. Moving them
    changes the public `/static/...` URLs the browser requests. Behavior-neutral for users,
    but out of scope for a phase that promises no observable change — and a missed reference
    means an unstyled page.
  - ⏸️ SL routes split across `routes/summer_league.py` and `routes/ui.py`, and the generically
    named but SL-only `/desk/tracker`: deferred to **Phase 5**. These change public URLs, so
    they need redirects and a deliberate decision, not a Phase 0 tidy-up.

---

# Bucket 6 — Reproducibility / idempotency ("reproduce themselves easier")

Mostly healthy — the version-flip rebuilds are already clean. Three gaps to close so a re-run
is a trustworthy recovery tool rather than a gamble.

- **Good today (leave alone):** `build_sl_cohort_baselines.py` and
  `rebuild_summer_league_environment.py` both write a fresh version and atomically flip
  `is_active`/`is_current`; never mutate prior rows; the latter has an explicit `--rollback`
  mode. Snapshot materialization is `on_conflict_do_update` over a fixed key matrix (no stale
  rows unless the matrix shrinks).
- **6.1 — `rebuild_sl_metrics` is a full unscoped wipe** (`metrics.py:1443-1446`). Re-runnable but
  heavy and briefly empties tables inside the txn (ties to Bucket 3). **Collapse to:** the
  append-only, dated version-flip publish of **Issue B / 1.6** — the single change that fixes the
  operational fire, the reproducibility gap, *and* unlocks longitudinal history. 🟡 → doc #3 (op)
  / doc #2 (materialization shape).
- **6.2 — Batch-progress can silently skip corrected files.** Durable per-game markers
  (`_run_batched_phase:603-615`) mean a re-run will *not* reprocess a changed-but-already-marked
  game unless dirty-detection fires or an operator sets `SL_INGEST_FULL_RECONCILE`
  (`app/cli/summer_league_ingest_runner.py:731-822,759-762`). **Collapse to:** make dirty-detection the
  default reliable path so re-running always reprocesses genuinely-changed inputs. 🟢/🟡.
- **6.3 — `backfill_summer_league_backbone` ordering precondition.** ✅ **Done in Phase 0.**
  The five-stage order (raw fetch → audit → backfill → normalize → metrics) is now in the
  script docstring, and the "no raw manifests" error names the ordered commands plus
  `--raw-root` as the other route to the same failure, so a wrong path is not misdiagnosed as
  a missing fetch. 🟢.

---

# Recommended sequencing

Ordered so each wave de-risks the next and nothing splits duplicated code.

**Wave 0 — free wins (🟢, this cycle).** 1.4 (double-uPER), 5.1 (dead Explorer branches), 5.4
(naming), 6.3 (backfill ordering doc). No behavior change; clears noise.

**Wave 1 — the leverage (🟢, mostly internal).** Bucket 1.1–1.3 stat consolidation + 1.2
percentile unify, guarded by golden-number tests (offline == live == leaderboard). This is the
prerequisite for doc #2's engine and for the god-file splits. Highest value/risk ratio in the
whole plan.

**Wave 2 — the operational fires (🟡, graduate to specs).** Bucket 3 mega-transaction split and
Bucket 2 freshness-watermark collapse. These are behavior-changing and get their own
verification in doc #3 — *not* bundled into cleanup. Start with the transaction hotspot if the
production confirmation points at it.

**Wave 3 — structure (🟢, after Wave 1).** Bucket 5.2 god-file decomposition, 5.3 DTOs, 4.1
roster dual-write (with doc #4), 6.1/6.2 idempotency hardening.

## Open questions for the owner

1. **Live timeouts — partially answered by incident #669 (July 22–23):** a long full-ingestion
   transaction holding the writer lock was observed live, Desk starved behind it, deploy
   migration queued into a public outage. Remaining: was the deployed image missing the shipped
   chunking fixes, or is the surviving mega-transaction
   (`app/cli/summer_league_ingest_runner.py:1308-1348`) the holder? (Roadmap Phase 1 entry gate.)
2. **Store vs. recompute → resolved as dated materialization.** Rather than a binary, Issue B /
   1.6 materializes as-of-dated snapshots (version-flip, like `environment_profiles`) and reads
   the latest — which also yields longitudinal history. Confirm appetite for the schema change
   and the within-event daily-trend product surface it unlocks.
3. **Second spoke shape:** confirmed as "next multi-day basketball competition, TBD" — enough to
   design the engine/backbone generically without freezing an Event Desk framework at N=1.
4. **Materialization primitive for SL metrics (doc #2 fork):** adopt the generic
   `MetricSnapshot`/`player_metric_values` versioning the rest of the app's offline analytics
   already use (reuse infra, more consolidation), or a per-table dated version-flip like
   `environment_profiles` (table-local, simpler shape match)? See Appendix A.

---

# Appendix A — P2 anti-pattern audit (app-wide)

Verified read-only sweep of `scripts/`, `app/services/`, `app/cli/` for destructive-overwrite
("wipe-and-recompute") data patterns, classified against P2. **Finding: the anti-pattern is
narrowly concentrated in the Summer League metrics rebuild; the rest of the app is already
longitudinal-first.**

### Offenders (fix under P2)

| path:line | what it wipes | version/as-of dim today? | severity |
|---|---|---|---|
| `metrics.py:1444-1446` (`rebuild`, unscoped; via `scripts/rebuild_sl_metrics.py`) | **all** `SummerLeaguePlayerSeason` + `MetricContext` + `MetricModel` rows | No (Season/Context); **MetricModel *is* versioned but is also deleted** | **HIGH** — destroys the computed advanced-metric basket *and* the auditable model-fit history every run |
| `metrics.py:1468-1475` (`rebuild`, scoped by competition; hourly desk tick) | same tables, scoped to touched competitions | No | **HIGH** — same destroy-and-repopulate; scope limits blast radius, not the missing time-axis. Per incident #669, scoping limits which rows are *persisted* while `compute()` still loads and recalculates the full historical dataset — so it bounds neither compute cost nor transaction length |
| `draft_order_service.py:96-100` (`bulk_replace_draft_order`) | all `DraftPickSlot` rows for a draft year (pick ownership, trades) | No (`created_at`/`updated_at` only) | **LOW** — admin-curated reference, not computed analytics; but trade/ownership history over a cycle is unrecoverable |

**Note on the top offender:** because `SummerLeagueMetricModel` was *designed* versioned/auditable
but is deleted wholesale, the P2 fix must also stop deleting prior `MetricModel` fits so the
history the table already models actually survives.

### Already compliant (the pattern to adopt) — leave alone

- **`MetricSnapshot` + `player_metric_values`** (`compute_metrics.py`, `compute_combine_scores.py`)
  — versioned `run_key`/`version`/`is_current`; deletes only under explicit `--replace-run`.
  **This is the reusable primitive SL metrics should align to.**
- **`compute_similarity.py`** (KNN comps) — recomputes derived comps scoped to an immutable
  `MetricSnapshot` version; a new run targets a new snapshot.
- **`consensus_service.py`** — consensus blend writes append-only snapshots (`computed_at`);
  schema docstring: "append-only, never updated."
- **`environment_profiles`** and **`cohort_baselines`** — textbook version-flip with atomic
  current-pointer and rollback.

### Cache-exempt (regenerable presentation; overwrite-in-place OK)

`event_desk_render_snapshots` (stamps upstream watermark ✓), SL Desk T3/T4 storylines & slate,
desk player grades (version-keyed upsert), `batch_progress` (transient pipeline state), video
manual-mention reconciliation.

### Out of scope (not recompute wipes)

Admin entity deletions (`admin_player_service`, `admin_auth_service`), player-merge/dedup cleanups
(`deduplicate_players.py`, `merge_*_dup_players.py`), and seed/demo fixtures — all delete specific
chosen rows on explicit action, not history on recompute.
