# Phase 2 — Stat Engine Consolidation: ticket breakdown

**Source of truth for scope:** `summer-league-remediation-roadmap.md` Phase 2, which draws its
detail from `summer-league-stat-engine-reuse-spec.md` (doc #2) §1–§4, the
`summer-league-simplification-backlog.md` (doc #1) items 1.1–1.3 and 1.5, and
`programmatic-code-discipline.md` §1.3 / §3.2.

**This doc is the ticket split, not new design.** Nothing here changes a decision already made
upstream. Where an upstream open question blocks a ticket, the ticket says so and proposes the
answer rather than silently picking one.

**Readiness:** Phase 1 is closed (#694, #697, #698, #699 merged). #701 is open but is Phase 1
work despite its title. Phase 2 is behavior-preserving and does not depend on #701.

**Live tickets:** GitHub Project #21, master spec issue #720.

| Ticket | Issue | Depends on |
|---|---|---|
| T1 — golden-number parity harness | #721 | — |
| T2 — lift the engine to `app/services/stats/` | #722 | — |
| T3 — one percentile utility | #723 | — |
| T7 — metric registry | #724 | #722 |
| T4 — one per-36/per-100 scaling definition | #725 | #721, #722 |
| T5 — Python recompute sites → engine | #726 | #725 |
| T6 — SQL push-down sites → registry | #727 | #726, #724 |
| T8 — capability model | #728 | #727 |
| T8b — adopt `rollup_class` | #729 | #728 |
| T9 — stat-constant confinement rule | #730 | #727, #724 |
| QA gate | #731 | all |

The T4→T5→T6→T8→T8b chain is serialized by **file overlap**, not by logic: all five modify
`app/services/summer_league_explorer_service.py`, and parallel tickets merge from separate
worktrees.

---

## What Phase 2 actually is

One sentence: **every box-derived number in the app is computed by one engine, declared once in a
registry, and a lint rule stops the copies from regrowing.**

The engine already exists — `app/services/summer_league/metrics.py` computes every metric off a
neutral `Box` dataclass and `LeagueContext`, with no ORM types in its signatures. Phase 2 is
mostly **deleting copies and pointing surfaces at it**, not building something new. The one
genuinely new component is the capability model (§3 of doc #2).

### The duplication, verified at HEAD

The `0.44` free-throw coefficient alone appears at **28 sites across 8 modules**:

| Module | Sites | Kind |
|---|---|---|
| `summer_league_explorer_service.py` | 1358, 1612, 1684, 1732, 1742, 2518, 2524, 2613, 2681, 3270, 3508, 3511 | Python + SQLAlchemy expr + raw SQL strings |
| `summer_league/metrics.py` | 277, 424–430, 545, 550, 652, 688, 692–693 | canonical engine |
| `summer_league_metrics_service.py` | 195, 202, 219, 718 | leaderboard/career pooling |
| `summer_league_stats_service.py` | 255 | season aggregate |
| `summer_league_leaders_service.py` | 618 | leaders board |
| `summer_league_environment_service.py` | 1007, 1728–1730 | **deliberately frozen** — see T7 note |
| `summer_league_environment_registry.py` | 374–384 | declared formula text |

Plus: Game Score re-derived in SQL at `summer_league_explorer_service.py:2544` with the comment
*"Mirrors the arithmetic in `_compute_player_values`"*; per-36/per-100 scaling implemented twice
(`_compute_player_values:2467` Python, `_scaled_sort_expr:2541` SQL) and a third time inside
`rollup_recombinable:1326`; and two divergent percentile implementations
(`summer_league_environment_service.py:764`, `summer_league/cohort_baselines.py:243`) plus a
reverse-lookup variant (`summer_league/desk_grades.py:123`).

### Phase exit criteria (from the roadmap — do not soften)

- Parity tests green in CI.
- Every duplicate formula site deleted.
- **A formula change requires editing exactly one place**, mechanically enforced.

---

## Ticket sizing note

Each ticket below is scoped to one PR by a Sonnet-5 / GPT-5.6-class agent: a single conceptual
change, explicit file list, no cross-phase archaeology, and acceptance criteria that a test run
can settle. Line numbers are cited as of HEAD and every ticket instructs the agent to re-verify
them before editing — several of these files are actively churning.

## Dependency graph

```
T1 (parity harness) ─┬─────────────────────────────┐
                     │                             │
T2 (lift engine) ────┼──> T4 (scaling) ────────────┤
                     ├──> T5 (Python sites) ───────┼──> T9 (confinement rule)
                     ├──> T6 (SQL sites) ──────────┤
                     └──> T7 (registry) ──> T8 (capability)
                                        └──> T8b (rollup_class)
T3 (percentile utility) — independent, start anytime
```

T1, T2 and T3 can run in parallel on day one. T4/T5/T6 can run in parallel once T1 and T2 land.
T9 must land last: it is the ratchet that holds the cleanup in place, and it will fail while any
duplicate site survives.

---

## T1 — Golden-number parity harness

**Blocks:** T4, T5, T6. **Depends on:** nothing.

**Why first.** Doc #2 §4 and discipline §3.2 both make this the gate: the harness pins today's
values *before* any copy is deleted, so a consolidation that changes a number fails loudly
instead of silently shifting a user-visible stat.

**Scope.** A fixture-backed test asserting, for a fixed set of players and one competition:

```
engine value == stored column == Explorer cell == leaderboard value
```

for TS%, eFG%, TOV%, 3PAr, FTr, Game Score, and the per-36 / per-100 scaled forms.

- Fixture lives under `tests/fixtures/` and is deterministic — a small hand-built set of box
  lines with known totals, **not** a production dump.
- Test lives in `tests/integration/` (it must exercise the real Explorer and leaders read paths,
  not a mock of them) with the pure-engine leg in `tests/unit/`.
- The expected values are recorded as literals in the test, generated from current behavior and
  reviewed once. This is the pin; a later ticket changing one is the signal, not the noise.

**Explicitly out of scope.** Deleting or refactoring any formula site. This ticket only observes.

**Acceptance.**
- [ ] Harness runs in CI on every PR and is green at HEAD before any Phase 2 consolidation lands.
- [ ] Deliberately perturbing the `0.44` in `metrics.py` fails the harness — verified and stated
      in the PR description. A harness that cannot fail is not a harness.
- [ ] Covers all four surfaces (engine / stored column / Explorer / leaderboard) for at least one
      metric in each of the recombinable, additive-share, and pool-recalibrated rollup classes.

---

## T2 — Lift the pure engine to `app/services/stats/`

**Blocks:** T4, T5, T6, T7, T9. **Depends on:** nothing (can run parallel to T1).

**Why.** Doc #2 §1: the engine must stop living inside the Summer League package before other
spokes can call it. `app/services/stats/__init__.py` already exists as a documented, empty
placeholder with the import contract written **before** the code — the point being that the
engine cannot acquire a spoke dependency on its first day.

**Scope — pure move, zero behavior change.**

Move from `app/services/summer_league/metrics.py` to `app/services/stats/`:

- `Box` (renamed `StatInputs`) and `LeagueContext` (renamed `PoolContext`), with the old names
  kept as aliases so no caller breaks in this PR.
- Every pure function: `compute_metrics`, `compute_uper`, `compute_ortg`, `game_score`,
  `game_score_line`, `game_score_from_row`, and the ratio helpers.

**Stays in `app/services/summer_league/`:** `rebuild_sl_metrics` and everything DB-bound. The
seam is *pure function vs. orchestration*, not "everything in metrics.py".

**The one coupling to handle:** `Box.add_row(r)` pulls fields off a row by name. It is duck-typed,
so it does not import SL types — keep it that way. If it turns out to reference an SL-specific
column name, that field-mapping belongs in a thin SL adapter, not in the engine.

**Acceptance.**
- [ ] `app/services/summer_league/metrics.py` re-exports the moved names; no call site outside
      the engine changes in this PR.
- [ ] Import contract 3 (`app.services.stats` ↛ `app.services.summer_league*`,
      `app.schemas.summer_league*`) passes — it is already declared in `pyproject.toml` and
      currently green against an empty package; it must stay green against a populated one.
- [ ] `make precommit`, `mypy app --ignore-missing-imports`, and the T1 harness all green.
- [ ] No numeric output changes anywhere — assert via the T1 harness if it has landed, otherwise
      via the existing SL test suite.

---

## T3 — One percentile utility

**Blocks:** nothing. **Depends on:** nothing. Good parallel starter.

**Why.** Doc #1 item 1.2. Three implementations of the same interpolation, "consistent by
comment" rather than by code.

**Scope.** One `percentiles` utility under `app/services/stats/` providing forward
(value → percentile) and reverse (percentile → value) linear-interpolated lookup, matching
numpy's default `'linear'` method. Repoint all three call sites:

- `summer_league_environment_service.py:764` `_percentile`
- `summer_league/cohort_baselines.py:243` `compute_breakpoints`
- `summer_league/desk_grades.py:123` `percentile_of_value` (the reverse-lookup variant)

**Careful — these are not textually identical, and the difference is behavioral:**

| | env `_percentile` | `compute_breakpoints` |
|---|---|---|
| Input | `q` as 0–1 float | integer percentiles 0–100 |
| Rounding | none | `round(..., 2)` |
| Empty input | raises `ValueError` | returns `{}` |

The shared utility must be the **unrounded** primitive. Rounding and empty-handling stay at each
call site so no stored value shifts. A PR that pushes `round(..., 2)` into the shared helper is
wrong and will move `cohort_baselines` breakpoints' consumers.

**Acceptance.**
- [ ] One implementation; the other two deleted, not wrapped.
- [ ] Unit tests cover: single-element input, exact-rank input, interpolated-rank input, empty
      input per each caller's contract, and forward/reverse round-trip.
- [ ] Existing `cohort_baselines` and environment-profile tests pass unchanged — byte-identical
      stored breakpoints.

---

## T4 — One per-36 / per-100 scaling definition

**Depends on:** T1, T2.

**Why.** Doc #1 item 1.3. The same scaling arithmetic exists three times, two of them explicitly
documented as mirrors of the third.

**Scope.** A single scaling definition under `app/services/stats/`, consumed by:

- `summer_league_explorer_service.py:2467` `_compute_player_values` (Python display path)
- `summer_league_explorer_service.py:2541` `_scaled_sort_expr` (SQL sort path — comment says
  *"Mirrors the arithmetic in `_compute_player_values`"*)
- `summer_league_explorer_service.py:1326` `rollup_recombinable` (re-derives `pts_per100`)

**Watch the known gotchas** (both already cost this repo a bug):

- **Summer League pace is per-48, not per-40.** The extrapolation constant is not the NBA one.
- **The SQL-sort `COALESCE` gotcha** — the sort expression's null handling must match the display
  path's, or a row sorts differently than it renders.

**Acceptance.**
- [ ] One definition; display and sort paths both call it.
- [ ] T1 harness green, including the scaled forms.
- [ ] A test asserts display order == sort order for a fixture containing null/zero-minute rows.

---

## T5 — Point the Python recompute sites at the engine

**Depends on:** T1, T2. Parallel with T4 and T6.

**Why.** Doc #1 item 1.1, the 🟢 half — pure behavior-preserving swaps, no query-shape change.

**Scope.** Delete the local re-implementations and call the engine at:

- `summer_league_explorer_service.py:1358` (Python rollup), `2518`, `2524`
- `summer_league_leaders_service.py:618`
- `summer_league_stats_service.py:255`
- `summer_league_metrics_service.py:192–219` (`_pooled_ts`, TOV denominator) and `718`

**Preserve the pooling semantics.** `_pooled_ts` sums components then re-computes — that is
correct and must stay correct; the ticket replaces the *formula*, not the aggregation strategy.
Career TOV% pooling is currently minute-weighted by a deliberate prior decision — do not change
it here. If unifying the formula would change that, stop and flag it rather than absorbing it.

**Acceptance.**
- [ ] Zero `0.44` literals remain in these four modules.
- [ ] T1 harness green; no value changes.
- [ ] `make perf` shows no route query-count regression.

---

## T6 — Point the SQL push-down sites at one definition

**Depends on:** T1, T2, and the T7 registry if T7 lands first. Parallel with T4/T5 otherwise.
**Class: 🟡 behavior-changing** — this one alters query shape and needs its own verification.

**Why.** Doc #1 item 1.1, the 🟡 half. The Explorer needs SQL for filter/sort at scale, so the
formulas are hand-written a fourth time as SQLAlchemy expressions and raw SQL strings.

**Upstream open question this ticket answers.** Doc #2 §4 leaves open: full formula→SQL emission,
or one-Python-one-SQL-per-metric bound together in the registry with a parity test?

**Recommendation: take the fallback.** Keep one Python form and one SQL form *per metric, declared
adjacently in the registry*, bound by a parity assertion. That is still one source of truth per
metric — down from eight — and it avoids building a formula→SQL compiler as a prerequisite to a
cleanup. Revisit emission only if a third form (e.g. a second spoke's dialect) appears.

**Scope.** Sites in `summer_league_explorer_service.py`:

- SQLAlchemy expressions: 1612, 1684, 1732, 1742
- Raw SQL strings: 2613, 2681, 3270, 3508, 3511
- Game Score SQL at 2544

**Acceptance.**
- [ ] Each SQL form is declared next to its Python form in one place; no formula appears in a
      string literal in the service.
- [ ] A test asserts, per metric, that the SQL form and the Python form return equal values over
      the T1 fixture — this is the binding, and it must be able to fail.
- [ ] `make explain ROUTE=<explorer>` against a prod-like DB confirms no plan regression: any
      query that used an Index Scan still does.
- [ ] `make perf` within budget.

---

## T7 — Metric registry

**Depends on:** T2. **Blocks:** T8, T8b, and the binding half of T6.

**Why.** Doc #2 §2 — declare each metric once. This is the spine that makes T6's binding and T8's
capability derivation possible, and it carries the Player Development Ledger's comparison
guardrails so the same declaration serves both.

**Scope.** A declarative registry under `app/services/stats/`, one entry per metric:

```
metric_key, metric_family, unit, denominator, definition_version,
requires, formula, rollup_class, grain_validity,
comparison_semantics, allowed_reference_kinds, minimum_sample_rule,
coverage_requirement, interpretation_note
```

**There is in-repo precedent to copy, not invent:**
`app/services/summer_league_environment_registry.py` already declares metrics this way
(formula text, denominator, interpretation at :374–384). Match its shape.

**`registry_version` is load-bearing.** Doc #2 §5 makes it the stamp on every materialized row in
Phase 3 — the field that answers *"did this number change because new games arrived, or because
we changed the formula?"*. It is not decoration.

**Note on the environment service's `turnover_rate`.** `summer_league_environment_service.py:1730`
computes `FGA + 0.44*FTA + TOV` under an explicit *"frozen contract formula (§4)"* comment — it is
deliberately independent of the pooled engine value. Do **not** silently repoint it. Either give
it a registry entry that preserves the frozen semantics, or record an explicit exemption with the
comment discipline §1.3 requires. Flag this in the PR; it is the one place where "delete the
duplicate" is the wrong reflex.

**Acceptance.**
- [ ] Every metric consolidated in T4/T5/T6 has exactly one registry entry.
- [ ] `rollup_class` values are declared for all of them (see T8b for adoption).
- [ ] `registry_version` is defined and readable by the materialization path Phase 3 will add.
- [ ] The frozen `turnover_rate` is either declared or exempted, explicitly, with reasoning.

---

## T8 — Capability model: computable = requires ∩ provides

**Depends on:** T7. **This is the one genuinely new component in Phase 2.**

**Why.** Doc #2 §3 / doc #1 item 1.5. There is no `metric → required inputs → source provides`
mapping today. Availability is expressed as coarse per-pool booleans plus inline null-checks, so a
metric is silently `None` at one call site and computed at another.

**Scope.**

- Each source/event declares which canonical inputs it provides (box only? PBP? shot locations?
  lineups?).
- Computable metrics are **derived**: `metric.requires ⊆ source.provides`. A metric whose inputs
  are unavailable is *structurally absent*, not conditionally null.
- The existing flags become inputs to that derivation rather than gates:
  `pbp_available`, `shotchart_available`, `adv_eligible`, `data_quality`
  (`summer_league/normalization.py:699-717,1553-1588`).
- Retire the ad-hoc inline gates — `astd_pct` at `summer_league_explorer_service.py:2581-2588` is
  the named example; find the rest.

**Acceptance.**
- [ ] A source declaring box-only inputs yields a metric set with no PBP-derived metrics in it,
      asserted by test.
- [ ] `astd_pct` availability comes from the derivation, not an inline check.
- [ ] No user-visible change: a metric absent today is still absent, and one present today is
      still present — pinned by the T1 harness plus an explicit before/after availability list in
      the PR description.

---

## T8b — Adopt `rollup_class` at the five hand-derived sites

**Depends on:** T7.

**Why.** Doc #2 §2 is blunt about this: the rollup taxonomy — recombinable-from-box-totals /
additive-share / pool-recalibrated — **has been re-derived by hand at least five times**, each
time as a comment plus bespoke code, and it has already produced two bugs (the SQL-sort `COALESCE`
gotcha; the ws82/vorp82 reclassification).

**Scope.** Replace the five hand-derivations with reads of the registry's `rollup_class`:

- the advanced-metrics wiring
- the Explorer's `rollup_recombinable` (`summer_league_explorer_service.py:1326`)
- Game Score surfaces (`game_score_line`)
- leaders venue/blend (`_blend_leader_values`, `summer_league_metrics_service.py:683`)
- the Class Tracker

**Acceptance.**
- [ ] Each of the five sites reads `rollup_class` rather than encoding the taxonomy locally.
- [ ] A test asserts the two historically-wrong classifications (ws82/vorp82 as rate_composite;
      the COALESCE-sensitive metric) are declared correctly and behave correctly.
- [ ] T1 harness green.

---

## T9 — Stat-constant confinement rule

**Depends on:** T4, T5, T6 (it will fail while any duplicate survives). **Lands last.**

**Why.** Discipline §1.3 states the purpose exactly: *"This makes doc #2's consolidation stick.
Without it, the eight copies regrow the next time someone needs a formula in a query."* This is
the ticket that converts Phase 2 from a cleanup into a permanent property.

**Scope.** An AST/lint checker in the existing `scripts/` guard family (alongside
`check_unscoped_delete.py` and `check_migration_safety.py`, both of which have unit tests to
model on — `tests/unit/test_check_unscoped_delete.py`, `test_check_migration_safety.py`):

- Designated stat coefficients — `0.44` and the Game Score weights — may appear **only** under
  `app/services/stats/`.
- Additionally flag SQL string literals matching stat-aggregate patterns (`SUM(fga)`,
  `2 * (fga`) outside that package.
- A ratcheted allowlist that may shrink but never grow, matching the established pattern in this
  repo. Any surviving entry (the frozen `turnover_rate`, if exempted in T7) carries a justifying
  comment.

**Acceptance.**
- [ ] Wired into `make lint` and CI as its own step.
- [ ] Deliberately introducing a `0.44` outside `app/services/stats/` fails the check — verified
      and stated in the PR.
- [ ] The allowlist is empty, or every entry has a named justification.
- [ ] **Phase 2 exit is provable here:** with this rule green, a formula change requires editing
      exactly one place, mechanically rather than by convention.

---

## Not in Phase 2 — deliberately

These belong to Phase 3 and must not be pulled forward; doing so freezes duplicated math into
tables, which is exactly what sequencing rule 3 forbids.

- Dated materialization / version stamps on the metrics tables (Issue B) — Phase 3.
- Switching the Explorer's default view from live recompute to reading `is_current` snapshots —
  Phase 3, and only safe *after* the formulas are unified.
- The within-event daily trend surface — Phase 3.
- Level-adjusted metric translation — Phase 2 supplies the engine it will live in, not the study.
