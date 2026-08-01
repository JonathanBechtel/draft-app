# Summer League Phase 3 — Longitudinal Payoff: Tech Spec

**Status:** Ready for QA-checklist + project generation.
**Reads with:** `summer-league-remediation-roadmap.md` (Phase 3 section — this spec is its detail), `north-star-architecture.md` (P1/P2/P4 govern every choice below), `summer-league-stat-engine-reuse-spec.md` (§5 read-switch, §6 trends).

**Roadmap alignment (journey-graph statement).** This phase deepens the existing longitudinal spine — versioned projections over canonical assertions — rather than adding any parallel store. It is the first user-visible payoff of P2 (longitudinal-first): advanced metrics become a queryable time series. Every deliverable is built *second-event-aware* (see Decisions D4): the read contracts take a scope, not a Summer League table name, so the next spoke (pre-season / G-League / college) inherits these surfaces instead of reimplementing them. Spoke #2 itself and the cross-event Player Development Ledger remain intentionally deferred (roadmap "Deferred by decision").

---

## 1. Context and preconditions

Phase 2 (PR #750) unified the stat engine under `app/services/stats/` with a 28-entry registry, capability model, and a golden-number parity harness in CI. Phase 1 (+ follow-ups through PR #751) delivered the versioned-projection substrate: `DatedVersionMixin` adopters with partial-unique `is_current` guards, a staleness-guarded version-flip publish, intra-day compaction retaining **daily close + current** per scope, and a durable input watermark.

**In-flight prerequisites (not part of this project):**

- **#701** (scoped per-tick compute) — operational cost containment; no ordering constraint on this spec except it churns `metrics.py`/`ComputeResult`, so T5 (backfill) should rebase on it if both are mid-flight.
- **#732** (canonical pace for per-100) — **number-changing. T5 and T6 must not start until #732 is merged**, so historical backfill and trend values are computed on final formulas. T1–T4 are unaffected.

## 2. Goals

1. The Explorer's **default full-competition grain** reads `is_current` snapshots instead of recomputing live; sub-season/recombinable grains (per-game, last-N, date filters) keep calling the shared engine.
2. The SL metrics tables' **version stamps become truthful**: `registry_version`/`calculation_version` sourced from the registry, `as_of` populated at publish, and the two stamp-adjacent integrity gaps closed (D2, D3).
3. A **within-event daily trend surface** (GmSc / TS% / BPM through-day) renders for every SL event 2017→present, powered by daily-close snapshot versions — **populated at ship, not dormant until July 2027** (D1).

## 3. Non-goals (named deferrals — do not build)

- **Cross-event / cross-stage trends** (Player Development Ledger). Deferred by decision; re-entry when two spokes share a player population.
- **Spoke #2** (another event's pipeline). Deferred; will be scoped alongside Phase 4.
- **Legacy snapshot stamps.** `metric_snapshots` / `player_image_snapshots` lack `registry_version`/`calculation_version`/`as_of`. Out of scope here (they belong to the draft-wide engine, not SL); tracked as its own ticket at project-generation time so the gap stays owned, not ambient.
- **Desk/event-framework generalization.** Deferred until a second event forces the seams.
- **Per-day (non-cumulative) trend values.** Five-game samples make daily composites noise; only cumulative-through-day is in scope (D5).

## 4. Settled decisions

- **D1 — Backfill, don't wait.** The trend axis is synthesized for historical events (2017–2026) by computing cumulative-through-day lines from `summer_league_player_game_logs` via the shared engine and writing them through a **non-promoting archival publication path**: rows land with `published_at` set and `is_current=False`, and the current pointer is never touched. Routing through `publish_metric_version` is explicitly forbidden — its staleness guard compares publication *sequence numbers*, so a freshly-allocated backfill version would demote the live current rows and promote a historical through-day snapshot. The archival path reuses the publisher's scoping/validation helpers but not its flip; a failed or partial run is invisible to readers by construction. One idempotent operator script in `scripts/`; the read path cannot tell backfilled from organic rows.
- **D6 — `as_of` is source currency; event day is its own field.** Per `DatedVersionMixin` and P4, `as_of` means the input watermark (max source-row timestamp) — for 2017 data that is whenever those rows were last ingested or corrected, not July 2017. Trend ordering and daily-close selection therefore key on a new **`effective_day`** column (the event calendar day, Eastern) added to the two metrics tables: organic publishes stamp it from the rebuild's input day, the backfill stamps historical days, and `as_of` keeps its freshness meaning everywhere. Compaction's day-partition derivation is aligned to `effective_day` in the same change so "daily close" means the same day on both paths.
- **D2 — Fix the inverted `is_current` default.** The two mixin adopters re-declare `is_current=True` (mixin says `False`, "invisible until flipped"). Align adopters with the mixin default and fix the fixtures that relied on `True`. Python-side default only; no migration.
- **D3 — Give `cohort_baselines` the flip guard.** `(cohort_key, is_active)` is indexed but not partial-unique — the only versioned-flip table without a DB-enforced one-active guard. Add the partial unique index (CIC + `autocommit_block`, §1.7 checker rules, idempotent per the create_all gotcha) with a dedup pass first.
- **D4 — Second-event-aware by construction.** The trend read service and API take `scope_key`/competition parameters and speak registry metric keys; no `summer_league_*` literals in the read contract, templates, or JS. The read-switch is expressed against the versioned-projection pattern (mixin fields + partial-unique `is_current`), not table names, in its service seam. New chart/share components take an event/scope parameter.
- **D5 — Cumulative-through-day with cohort context.** The trend chart plots through-day cumulative values with a cohort band (event median / IQR from the same snapshots) per the analytical-voice convention. Day-over-day deltas may annotate, never headline.

## 5. Design

### 5.1 Explorer read-switch (default grain → snapshots)

- **Seam:** the Explorer service's default full-competition fetch swaps its engine call for a snapshot read filtered `is_current` — the same query family 52 existing read sites already use. Sub-season grains keep the live-engine path; the grain router decides, not the caller.
- **Correctness:** extend the parity harness with a leg asserting default-grain Explorer output == current snapshot rows == live engine output for a seeded competition. This is the roadmap's exit criterion "match stored values by construction," made mechanical.
- **Performance:** the switch changes the page's query shape → `make perf` budgets must hold (bump consciously if the shape legitimately changes) and every new/changed query gets `make explain ROUTE=...` against a prod-like branch; missing indexes ship in the same change. The snapshot tables already carry the partial `is_current` indexes; the expected plan is Index Scan on those.
- **Freshness display:** the default grain surfaces the snapshot's `as_of` (P4: source currency, not process time) wherever the Explorer shows data recency.

### 5.2 Version-stamp integrity

- Re-point `DEFAULT_METRIC_REGISTRY_VERSION` / `DEFAULT_METRIC_CALCULATION_VERSION` at the registry's own constants (single source; the dual literal dies). Publish path stamps `as_of` = input watermark time (already computed by the gate) rather than leaving it `NULL`.
- D2 and D3 land here (same code neighborhood).
- Verification: unit tests pin that a registry version bump propagates to newly published rows without touching `app/schemas/`; migration round-trip on the local docker PG for D3.

### 5.3 Trend surface

- **Read service** (`app/services/` — engine-agnostic contract): `get_daily_trend(scope_key, player_id | cohort, metric_keys, event_window) -> series of (effective_day, value, cohort_band, as_of)`, selecting the latest published version per (scope, `effective_day`) — compaction's daily-close invariant, keyed on the new column (D6). Registry metric keys only; `as_of` rides along for freshness labeling, never for ordering.
- **Backfill script** (`scripts/backfill_sl_daily_trend_versions.py`): per historical event day, compute through-day lines from game logs via the shared engine and insert via the archival path (D1) — `is_current=False`, `published_at` set, `effective_day` = the historical day, `as_of` = the true source watermark of the inputs. Ships with a regression test proving a backfill run cannot alter any reader-visible current row. Idempotent on `(scope, version)`; re-run safe; respects the writer lock via the bounded acquire. Runs after #732. Dev first, prod via runbook note.
- **Surface:** a trend module on the SL player-page section (and event/competition page), chart in vanilla JS per the frontend rules, share-card variant reusing the share engine. Cumulative-through-day lines for GmSc / TS% / BPM with cohort band; `as_of` labeling; mobile-checked.
- **Volume sanity:** ~9 events × ≤4 competitions × ~10 days × roster ≈ low-hundreds-of-thousands of player-season rows *if naively per-player-per-day*. To bound growth, backfill publishes **per-day versions only for players with ≥1 game played through that day**, and compaction's retention already caps organic growth. The backfill ticket must state measured row counts before prod.

## 6. Work breakdown (ticket-shaped)

| # | Ticket | Depends on | Verification flavors | Key files |
|---|---|---|---|---|
| T1 | Version-stamp integrity: registry re-point, `as_of` at publish, D2 default fix + fixture repair | — | unit, integration | `summer_league_metrics.py` (schemas), `metric_publish.py`, fixtures |
| T2 | D3: `cohort_baselines` partial-unique active guard + dedup (migration, §1.7-compliant, idempotent) | — | unit, integration (migration round-trip) | `summer_league_desk.py`, new alembic revision |
| T3 | Explorer read-switch: default-grain snapshot reads, grain router, parity leg, `as_of` display | T1 | unit, integration, perf (`make perf` + `make explain`) | `summer_league_explorer_service.py`, parity tests, budgets |
| T4 | Trend read service + API endpoint (scope-parameterized, registry keys, daily-close selection on `effective_day`) + `effective_day` column migration (§1.7-compliant, idempotent) + organic-publish stamping + compaction day-partition alignment (D6) | T1 | unit, integration (migration round-trip) | new `app/services/` module, route, `app/models/` shapes, `summer_league_metrics.py`, `metric_publish.py`, `metric_compaction.py`, new alembic revision |
| T5 | Archival publication path (D1: non-promoting, cannot-demote-current regression test) + historical backfill script + measured row counts + runbook note (**gated on #732 merged**) | T4 | integration (docker PG), idempotency + non-promotion tests | `metric_publish.py` (archival entry point), new `scripts/` module |
| T6 | Trend surface UI + share card (cumulative-through-day, cohort band, mobile) | T4, T5 | integration, e2e, visual | templates, `app/static/`, share engine |
| T7 | QA gate: full suite, parity green, perf budgets, spec compliance, visual baseline | all | all | — |

File-overlap notes for `/create-project`: T1 and T2 touch adjacent schema modules but different files — parallel-safe. T3 and T4 are parallel after T1 (different services). T6 is the only UI ticket (e2e + visual flavors; anonymous role).

## 7. Exit criteria (mapped to roadmap)

1. A player's advanced line is queryable as a time series for every SL event 2017→present (not just future events).
2. Explorer default-grain values match stored snapshots **by construction** (parity leg green in CI); sub-season grains match the engine via the existing harness.
3. The trend surface renders from retained history on player and event pages, with cohort context and honest `as_of` labeling (source currency, distinct from `effective_day` ordering).
3a. A backfill run provably cannot alter reader-visible current rows (non-promotion regression test green).
4. `registry_version` has one source of truth; a formula-registry version bump is visible in the next published rows.
5. Page query budgets hold (or are consciously bumped) and every new query is index-scanned on a prod-like branch.

## 8. Product QA (browser-verifiable behaviors)

A standalone QA checklist is deliberately skipped for this phase — only one ticket carries a customer-facing surface. These behaviors are the Playwright-verifiable contract; `/create-project` maps items 1–3 into T3's e2e lines, 4–11 into T6's e2e/visual recipes, and all of them into the T7 gate. All flows are anonymous (no login recipe needed).

**T3 — Explorer read-switch (correct outcome: nothing visibly changes except freshness):**
1. The default full-competition Explorer view renders the same values after the switch (spot-check a stable historical competition against pre-switch capture).
2. An `as_of` freshness label is visible on the default grain.
3. A sub-season grain (per-game / last-N / date filter) still returns rows and plainly exercises the live-engine path (values change with the filter).

**T6 — Trend surface (desktop + mobile widths, both themes):**
4. A historical-event participant's SL player-page section shows the trend chart with cumulative through-day GmSc / TS% / BPM lines.
5. The cohort band renders and stays legible at mobile width and in dark theme.
6. A one-game player renders a sensible single-point state, not a broken chart.
7. A player with no SL games shows no trend module at all (no empty shell).
8. The event/competition page renders the scope-level trend module.
9. The freshness label reflects `as_of` (source currency), not the event day.
10. The share-card export produces a clean PNG of the trend (infographic-qa standards: containment, legibility at thumbnail size).
11. At mobile width the chart is contained (no horizontal page scroll) and interactive elements respond to tap.

## 9. Risks

- **#732 timing** gates T5/T6; if it slips, ship T1–T4 and hold the backfill — the surface must not ship on numbers that are about to move.
- **Backfill volume** is estimated, not measured; T5's first deliverable is the measured count on dev before any prod write.
- **Snapshot staleness on the default grain** is bounded by the rebuild cadence (hourly in-event, watermark-gated off-season); the `as_of` display makes it honest rather than hidden.
- **Two-engine divergence** (SL stack vs. draft-wide `MetricSnapshot` machinery) widens with this phase. Accepted knowingly; convergence intent lives in `summer-league-stat-engine-reuse-spec.md`, and the legacy-stamp ticket keeps the gap owned.
