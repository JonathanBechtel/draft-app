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

## Guiding principle (from doc #5)

> Keep one durable canonical record. Everything users see is a thin, disposable projection
> computed *from* that record through *one* code path, each carrying an explicit watermark.

Every redundancy below is a violation of that principle in one of two directions: the same
computation done N times (stat math, percentiles), or the same fact stored N times and
allowed to drift (roster status, freshness clocks, offline-vs-live stat columns).

## Current-state note — battles already won (do NOT re-fight)

The postmortem predates these. Confirmed present at HEAD; treat as **done**, build on them:

- **Gemini/embedding calls no longer run inside the writer lock.** Identity resolution is
  split into a lock-free *preparation* pass (all Gemini calls) and small locked write batches
  (`RESOLUTION_BATCH_SIZE = 8`). See `player_resolution.py:719-723`,
  `summer_league_ingest_runner.py:656-728`.
- **The 87-minute whole-venue transaction is chunked.** Shot/PBP normalization runs
  `EVENT_BATCH_SIZE = 8` games per `db.begin()`, releasing the lock between batches
  (`summer_league_ingest_runner.py:548-653`).
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

### 1.4 — Micro-bug: uPER computed twice identically 🟢

- **Evidence.** `metrics.py:728-732` calls `compute_uper(...)` and immediately recomputes the
  identical value on the next lines. Harmless but wasteful and confusing.
- **Collapse to.** Compute once. Trivial.
- **Class.** 🟢.

### 1.5 — (Net-new, not a dedupe) capability map: `metric → required inputs`

- **Gap.** There is no `metric → required inputs → source provides` mapping. Availability is
  expressed today as coarse per-pool booleans (`pbp_available`, `shotchart_available`,
  `adv_eligible`, `data_quality` in `normalization.py:699-717,1553-1588`) plus scattered inline
  `None`-when-missing checks (e.g. `astd_pct` gated ad hoc at `explorer_service.py:2581-2588`).
- **This is the one genuinely new piece** — it belongs to doc #2 and is what lets the engine
  answer "given this source's inputs, which metrics are computable" mechanically instead of by
  hard-coded assumption. Listed here so the dedupe work in 1.1–1.3 is designed to hang off it.

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
large critical section remains, and it is the leading suspect for the transaction timeouts
still observed in production.

- **Evidence — remaining mega-transaction.** `summer_league_ingest_runner.py:1308-1348` wraps,
  inside **one** `db.begin()` under the writer lock:
  `rebuild_sl_metrics(db)` (a *full unscoped table wipe* — `metrics.py:1443-1446` deletes all of
  `SummerLeaguePlayerSeason` / `MetricContext` / `MetricModel`) → `materialize_desk_render_snapshots`
  (72 variants) → `refresh_environment_profiles_for_year`. The lock is held for the entire
  rebuild + materialization + environment refresh.
- **Why it's coupled by design.** `write_lock.py:1-7` states the coupling in a comment: *"Every
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
  makes the stored column and the recompute provably equal; this item tracks *deciding* whether
  to store-and-read or always-recompute once they can't diverge. 🟡 → doc #2.

---

# Bucket 5 — Dead weight & decomposition 🟢

All safe hygiene. **Do the god-file splits *after* Bucket 1**, or you'll just relocate
duplicated math into smaller files.

- **5.1 — Stubbed Explorer subjects.** Teams/games subjects return `available=False` ("Phase 1
  = players only") — dead-ish branches carried inside the 198KB Explorer file. Remove or
  feature-gate cleanly.
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
- **5.4 — Naming/layout inconsistencies.** `stats/summer-league/` (hyphen) vs
  `summer_league/desk/` (underscore) template dirs; `static/` root vs `static/js|css/` asset
  placement; SL routes split across `routes/summer_league.py` and `routes/ui.py` with no shared
  prefix; `/desk/tracker` is generically named but SL-only. Normalize conventions.

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
  heavy and briefly empties tables inside the txn (ties to Bucket 3). **Collapse to:** scoped
  rebuild + atomic publish. 🟡 → doc #3.
- **6.2 — Batch-progress can silently skip corrected files.** Durable per-game markers
  (`_run_batched_phase:603-615`) mean a re-run will *not* reprocess a changed-but-already-marked
  game unless dirty-detection fires or an operator sets `SL_INGEST_FULL_RECONCILE`
  (`summer_league_ingest_runner.py:731-822,759-762`). **Collapse to:** make dirty-detection the
  default reliable path so re-running always reprocesses genuinely-changed inputs. 🟢/🟡.
- **6.3 — `backfill_summer_league_backbone` ordering precondition.** Hard-fails without raw
  manifests (raw fetch → audit → backfill → normalize → metrics). Document the required order
  explicitly and make the failure message actionable. 🟢.

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

1. **Live timeouts:** is the mega-transaction (`ingest_runner.py:1308-1348`) the cause, or are
   the shipped lock/transaction fixes simply not on the deployed image? (Bucket 3 action item.)
2. **Store vs. recompute:** once one formula makes them equal, do we keep the materialized stat
   columns (fast reads) or always recompute live (simpler, one path)? (Bucket 4.2.)
3. **Second spoke shape:** confirmed as "next multi-day basketball competition, TBD" — enough to
   design the engine/backbone generically without freezing an Event Desk framework at N=1.
