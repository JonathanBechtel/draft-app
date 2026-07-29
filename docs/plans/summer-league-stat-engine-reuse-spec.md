# Stat-Engine Reuse Spec (doc #2)

**Status:** Design spec. Strategy document — no code changes proposed inline.

**Part of the five-doc set** (see `summer-league-simplification-backlog.md` doc #1 for the map).
This doc owns: the source-agnostic stat engine, the metric registry + capability model, the
single offline/real-time compute path, and the dated-materialization decision (Issue B).

**Governed by** the two principles in doc #1:

- **P1** — one canonical record; projections are thin readers through one code path.
- **P2** — longitudinal-first: retain history by default; destructive rebuild is the exception.

## Why this doc is the leverage point

The Explorer is the most-used Summer League surface, and dynamic advanced-stat calculation is its
value. Yet the math behind it is implemented many times over. Consolidating it is the single
highest value/risk item in the whole program: the reusable engine **already exists**, so most of
the work is deleting copies and pointing surfaces at it — not building something new.

Two named tickets (from doc #1 Bucket 1), kept separate:

- **Issue A — dedupe-and-lift** (behavior-preserving; Wave 1): consolidate the math; every
  surface calls one engine.
- **Issue B — materialize-and-read, dated** (depends on A): read precalculated values off dated
  snapshots instead of recomputing live; unlocks longitudinal history.

## Current state (evidence)

Verified against HEAD `c78af8d`.

**The engine exists and is already source-decoupled.** `app/services/summer_league/metrics.py`
computes every metric off a neutral `Box` dataclass (`metrics.py:200-220`) and `LeagueContext`
(`metrics.py:245-264`) — not off SL ORM rows. Signatures are shape-agnostic (`compute_metrics`,
`compute_uper`, `compute_ortg`, `game_score(b: Box)`). The **only** coupling point is
`Box.add_row(r)` (`metrics.py:222`), which pulls fields off a row by name — i.e. re-pointing the
engine at a new source means feeding a different `Box`.

**Its formulas are re-implemented from scratch at request time in four other places:**

- **TS% denominator `2·(FGA + 0.44·FTA)` appears at ≥8 sites** — canonical `metrics.py:650`;
  Explorer Python `explorer_service.py:1365,2568`; Explorer SQL strings `2663,2731,3321,3560`;
  Explorer filter/sort `1619,1691,1739`; metrics-service `summer_league_metrics_service.py:192-203,718`.
- eFG%, TOV%, 3PAr, FTr follow the same multi-site pattern.
- **Game Score** re-derived in SQL at `explorer_service.py:2610-2624` with a comment: *"Mirrors
  `game_score` exactly."*
- **Per-36/per-100 scaling** implemented twice — `_compute_player_values` (Python,
  `explorer_service.py:2527-2541`) and `_scaled_sort_expr` (SQL, `2591-2607`, comment: *"Mirrors
  the arithmetic in `_compute_player_values`."*).
- **Percentile normalizer copy-duplicated** — `environment_service.py:764-776` and
  `cohort_baselines.py:243-283` (plus a reverse-lookup variant at `desk_grades.py:123`). No
  z-score/stdev normalization exists; normalization is entirely percentile-based.

**Consequence:** changing one coefficient (e.g. the 0.44) requires editing ~8 sites, or the
offline column and the live Explorer cell silently disagree — the stats-domain twin of the Desk's
prose-vs-metric contradiction.

**No capability model.** Availability is expressed today as coarse per-pool booleans
(`pbp_available`, `shotchart_available`, `adv_eligible`, `data_quality` in
`normalization.py:699-717,1553-1588`) plus scattered inline `None`-when-missing checks (e.g.
`astd_pct` gated ad hoc at `explorer_service.py:2581-2588`). There is no
`metric → required inputs → source provides` mapping.

**A longitudinal materialization primitive already exists app-wide** (doc #1 Appendix A):
`MetricSnapshot` + `player_metric_values` (`run_key`/`version`/`is_current`) power the non-SL
offline analytics. The SL metrics rebuild is the one pipeline that never adopted it and instead
full-wipes (`metrics.py:1443-1446`).

## Target architecture

### 1. The engine — pure functions over canonical inputs

Generalize the existing `Box`/`LeagueContext` into a neutral input contract that is not
SL-specific:

- **`StatInputs`** — canonical box/rate inputs (minutes, made/attempted by type, rebounds,
  assists, turnovers, possessions, optional PBP-derived counts). This is today's `Box`, renamed
  and lifted out of the `summer_league` package to a shared `app/services/stats/` home.
- **`PoolContext`** — the league/competition-relative context (league rates, pace, calibration
  eligibility) that composite metrics need. This is today's `LeagueContext`.
- Every metric is a **pure function** of `(StatInputs[, teammates/opponent StatInputs][,
  PoolContext])`. No ORM types, no SL column names, no I/O.

Re-pointing at a second spoke (or combine/college/NBA) = writing a small **adapter** that builds
`StatInputs` from that source's rows. Nothing else in the engine changes. This is the concrete
mechanism behind "one master longitudinal record, sources are adapters."

### 2. Metric registry — declare each metric once

A declarative registry (one entry per metric) is the spine. Each entry carries:

```text
metric_key, metric_family, unit, denominator, definition_version,
requires (canonical inputs it needs), formula (single definition),
rollup_class (recombinable | additive_share | pool_recalibrated), grain_validity,
comparison_semantics, allowed_reference_kinds, minimum_sample_rule,
coverage_requirement, interpretation_note
```

**Rollup semantics belong in the registry too — this taxonomy has been re-derived by hand at
least five times.** How a metric aggregates across grains — **recombinable-from-box-totals**
(recompute from summed components), **additive-share** (sum then re-share), **pool-recalibrated
rate/composite** (must be recomputed against pool context, never averaged) — shows up
independently in the advanced-metrics wiring, the Explorer's advanced metrics
(`rollup_recombinable`), the Game Score surfaces (`game_score_line`), leaders venue/blend
(`_blend_leader_values`), and the Class Tracker, each time as a comment plus bespoke code. It has
already produced at least two bugs (the SQL-sort `COALESCE` gotcha; the ws82/vorp82
reclassification). `rollup_class` plus per-grain validity (career / competition / game) makes it
one declared fact every surface reads instead of five hand-derivations that can disagree.

The `comparison_semantics` / `allowed_reference_kinds` / `minimum_sample_rule` fields are taken
directly from the **Player Development Ledger** design
(`docs/plans/player-longitudinal-evidence-layer-pitch.md`) so the same registry that de-dupes the
math also carries the guardrails that stop a SL PER being charted against an NBA PER. One registry,
two payoffs.

### 3. Capability model — computable = requires ∩ provides

Replace the coarse booleans and inline null-checks with an explicit source-capability declaration:

- Each **source/event** declares which canonical inputs it provides (box only? PBP? shot
  locations? lineups?).
- **Computable metrics are derived**: `metric.requires ⊆ source.provides`. A metric whose inputs
  aren't available is *structurally* absent, not silently `None` at one call site and computed at
  another.
- The existing pool flags (`pbp_available`, `shotchart_available`, `adv_eligible`) become the
  *inputs* to this derivation rather than ad-hoc gates sprinkled through the Explorer.

This is the one genuinely new component. It's also what makes the engine honest across sources:
the second spoke gets exactly the metrics its data supports, computed identically.

### 4. Single compute path — offline == real-time

The core invariant: **a number computed in the Explorer must equal the number materialized
offline must equal the number on a leaderboard.** Achieve it by making all three call the engine:

- **Offline materializer** calls the engine and writes dated snapshots (§5).
- **Explorer / leaderboards** call the same engine functions for display values.
- **SQL push-down** (Explorer needs it for filter/sort at scale): emit the SQL expression from the
  *same* registry formula definition rather than hand-writing it a fourth time. If full
  formula→SQL emission is too heavy initially, the pragmatic fallback is: keep one Python form and
  one SQL form **per metric in the registry**, bound together, with a golden-number test asserting
  they agree — still one source of truth per metric, down from ~8.

**Guardrail:** golden-number parity tests that assert engine value == stored column == Explorer
cell == leaderboard value for a fixed fixture, run before any copy is deleted and kept in CI.

### 5. Materialization (Issue B) — dated snapshots, and the primitive fork

Per P2, SL player metrics become **append-only, as-of-dated** with an atomic current pointer, so
the Explorer reads precalculated values *and* a daily time series exists. The open decision is
**which primitive**:

| Option | Pros | Cons |
|---|---|---|
| **A. Reuse `MetricSnapshot` / `player_metric_values`** | Max consolidation — one materialization pattern app-wide | `player_metric_values` is a tall/long shape (row per player×metric×version); SL's advanced line is a **wide** row (many columns per player-competition) — a real remodel with read-perf cost on the Explorer's wide scans. **And its conventions are dated — see below.** |
| **B. Table-local dated version-flip** (like `environment_profiles`) | Simple shape match to the existing wide `summer_league_player_seasons`; fastest to land; proven in-repo | Leaves two materialization patterns in the app |

**DECIDED — option B: table-local dated version-flip.** Keep the wide
`summer_league_player_seasons` shape (it fits the Explorer's access pattern) and give it an
append-only dated version-flip with an atomic current pointer. Folding into `player_metric_values`
is **not** pursued. Settled — a constraint for downstream docs and tickets, not an open question.

### Which conventions to copy — `environment_profiles`, NOT `MetricSnapshot`

The repo contains **three generations** of temporal/versioning design. Breadth of use is not
quality: `MetricSnapshot` is the most widely used *and* the weakest, because it predates the
lessons the later designs absorbed.

| | `MetricSnapshot` (`app/schemas/metrics.py:61`) | `environment_profiles` (`app/schemas/summer_league_environment.py`) |
|---|---|---|
| **Scope model** | **Hardcoded into columns** — `cohort`, `season_id`, `position_scope_parent`, `position_scope_fine`. Built for the pre-draft/combine cohort case; position scope is meaningless for a competition-relative pool | **Generic** `scope_key` / `scope_kind` — extends to any scope without schema change |
| **Version stamps** | **One** integer `version` | **Three, explicitly "never conflate":** `version` (publication sequence), `registry_version` (metric-definition/formula version), `calculation_version` (pipeline/pooling logic version) |
| **Run identity** | Free-text `run_key` (e.g. `'2024_pre_draft_v1'`) — a human-typed convention | Structured scope + version |
| **Retention posture** | Carries `expires_at` — a TTL/cache smell, at odds with P2 retain-by-default | Versions retained; rollback path exists |

**Why the version-stamp difference is decisive for P2.** With a single integer you cannot answer
the fundamental longitudinal question: *did this number change because new games arrived, or
because we changed the formula?* A time series whose points were produced by silently different
formulas is not a time series — it is a trap. Separating publication / registry / calculation
versions is what makes the series interpretable, and is the direct analogue of doc #1's watermark
discipline applied to computation rather than freshness.

**Therefore the SL metrics tables should carry:**

- `scope_key` / `scope_kind` style generic scoping (not hardcoded cohort/position columns);
- the **three separate version stamps** (`version`, `registry_version`, `calculation_version`);
- `is_current` with a partial unique index for the atomic pointer;
- **an as-of date for the underlying data** — distinct from `calculated_at`/computed-at. This is
  the one field to verify against `environment_profiles` and **add if absent there too**: a
  longitudinal point needs to know *as of what state of the source data* it was true, not merely
  when the job ran. Without it we repeat the Desk's "a timestamp that records when a process ran,
  not when the data became current" defect in the stats layer.
- **no `expires_at`/TTL** semantics.

`registry_version` should be sourced from the metric registry in §2, which makes the registry and
the materialization mutually reinforcing: one declaration of each formula, and every stored row
stamped with which declaration produced it.

### Retention granularity — compaction, not TTL

P2 bans TTLs, but the rebuild runs on the **hourly** tick during events, and hourly versions are
operational churn, not analytical history — the meaningful longitudinal grain is the **daily
close**. Without a stated policy the table either grows unboundedly through a multi-week event or
invites an ad-hoc wipe under deadline pressure — the exact failure P2 exists to prevent.

**Policy: retain each as-of day's published close plus the current version; superseded *intra-day*
versions may be compacted once a newer version from the same as-of day is current.** An
unpublished candidate is retained separately until it is either published or superseded by a
newer candidate, so an in-flight or abandoned rebuild can never displace the durable close.
Compaction is a scoped, explainable delete of redundant intra-day versions — never the published
daily close, never the row the current pointer targets — and is therefore P2-compliant where a
TTL or full wipe is not. (It will also need an allowlist entry in the unscoped-`delete()` checker,
with the justifying comment discipline §1.1 requires.)

Revisit only if a sub-daily product surface appears; nothing currently designed needs finer than
daily history.

Issue 698 implementation note: the retention policy is enforced by a separate daily
compaction job. It keeps the latest published projection and latest unpublished candidate for
each closed UTC source day, the current pointer, and any undated legacy rows; it does not run
inside metric rebuilds. This keeps the dated Summer League projection as a durable spoke in the
Global Player-Journey Graph while removing only operational hourly churn, so a later trend
projection can still read one point per event day.

### Read-switch scope — what the snapshot serves, and what it cannot

The dated snapshot is **(player, competition)-grain**. It serves the Explorer's default
full-competition view — the read-switch in phasing step 5 applies there. Sub-season and
recombinable grains (per-game rows, last-N windows, date filters — e.g. `rollup_recombinable`
and the game-grain SQL forms) **cannot** be served from a season-grain snapshot and continue to
call the engine live. That split stays correct by construction: both paths share the one engine,
and the golden-number harness pins them equal. The §4 invariant is unchanged; only the
materialized-read optimization is scoped.

**Also (from the audit):** stop deleting `SummerLeagueMetricModel` on rebuild — it is already
versioned/auditable by design (`metrics.py:1446` currently wipes it), so preserving its fit history
is part of this change.

## Phasing (strangler, not big-bang)

1. **Lift the engine** to `app/services/stats/` behind its current signatures; no behavior change.
   Fix the double-uPER compute (`metrics.py:728-732`) in passing.
2. **Golden-number harness** — pin current Explorer/leaderboard/stored values for a fixture set.
3. **Point surfaces at the engine one at a time** (Explorer display → metrics-service leaderboards
   → filter/sort), deleting each duplicated copy only after its golden test passes. (Issue A done.)
4. **Registry + capability model** — declare metrics once; derive computable sets; retire the
   inline null-checks.
5. **Dated materialization** (Issue B) — add versioning/as-of to the metrics tables; switch the
   Explorer's default full-competition view from live recompute to reading `is_current` (§5
   read-switch scope — sub-season grains stay on the live engine); remove the full-wipe
   (coordinates with doc #3's transaction work). Keep engine parity tests green throughout.
6. **Within-event daily-trend surface** — the first product payoff of the time series (a small
   player-page trend of GmSc/TS%/BPM across the event). Cross-stage career ledger remains the
   larger, separate initiative in the longitudinal-evidence pitch.

## How this serves the roadmap

- **Second spoke:** a new competition needs only a `StatInputs` adapter + a capability
  declaration; it inherits every metric its data supports, computed identically. No forked math.
- **Longitudinal-first (P2):** dated materialization makes the metrics table a time series and one
  well-formed stage in the Player Development Ledger.
- **Consolidation (P1):** one engine, one registry, one compute path — the structural cure for the
  offline-vs-live divergence class of bug.

## Decisions made

- **Materialization primitive — SETTLED:** table-local dated version-flip on
  `summer_league_player_seasons`. Not `player_metric_values`. (§5)
- **Conventions — SETTLED:** copy `environment_profiles` (generic scope, three separate version
  stamps, no TTL), **not** `MetricSnapshot`, which predates those lessons. (§5)
- **Retention granularity — working default:** daily close + current; intra-day versions
  compactable. Revisit only if a sub-daily product surface appears. (§5)
- **Read-switch scope — SETTLED:** materialized reads serve the full-competition default grain;
  sub-season/recombinable grains stay on the live engine, pinned equal by the parity harness. (§5)

## Open questions

1. **SQL push-down** — invest in formula→SQL emission now, or the one-Python-one-SQL-per-metric
   fallback with parity tests? (§4)
2. **Daily-trend scope** — is the within-event daily trend a near-term product surface to spec, or
   just a byproduct we retain until the full Ledger ships? (§6)
