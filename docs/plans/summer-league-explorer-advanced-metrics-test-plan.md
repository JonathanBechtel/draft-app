# Summer League Explorer — Advanced Metrics Test Plan

**Sources:**
- Tech spec: `docs/plans/summer-league-explorer-advanced-metrics.md`

**Sibling artifact:** QA checklist at `summer-league-explorer-advanced-metrics-qa-checklist.md`

## Purpose

The product risk is **statistical misrepresentation and data regression**, not missing
pixels. Advanced composites are recalibrated per competition pool, so pooling them
incorrectly silently corrupts every multi-pool view; and switching the player career
grain off raw game-log sums onto `summer_league_player_seasons` risks changing box-stat
values users already trust. Tests must lock down (a) the three roll-up math paths, (b)
lossless box parity, (c) graceful handling of invalid sort/filter input, and (d) the
query budget. Browser tests confirm the metrics are actually reachable, sortable,
filterable, and honestly labeled in the live UI.

Repo conventions (see `docs/plans/ai-orchestrator-ticket-spec.md`):
- Conda env `draftguru`; tests via `conda run -n draftguru --no-capture-output python -m pytest <path>`.
- Unit tests: `tests/unit/` (no DB). Integration: `tests/integration/` (needs `TEST_DATABASE_URL` + `PYTEST_ALLOW_DB=1`; wrap shell with `scripts/with-db-env.sh`).
- Perf: `make perf`. Explain: `make explain ROUTE=...`. Visual: `make visual` → `tests/visual/screenshots/`.
- Run integration with `GEMINI_API_KEY= GEMINI_SUMMARIZATION_API_KEY=` to dodge the embedding-listener flakiness.
- Existing explorer suite: `tests/integration/test_summer_league_explorer.py` — extend it.

## Required Build-Time Tests

| Requirement | Test Type | Suggested Test | Ticket Mapping |
|---|---|---|---|
| Column catalog classifies every advanced col into exactly one bucket with a rollup fn | unit | `tests/unit/test_sl_explorer_catalog.py` — assert catalog completeness; each `summer_league_player_seasons` advanced col present, has bucket ∈ {recombinable, additive, rate_composite}, sortable/filterable flags set | create-project |
| Exact-recombinable roll-up (TS%/eFG%/3PAr/FTr/pts100) recomputes from summed components | unit | `tests/unit/test_sl_explorer_rollups.py::test_recombinable` — feed multi-comp box totals, assert formula-based result (not mean) | create-project |
| Additive shares (WS/OWS/DWS/VORP/GameScore) sum across pools | unit | `...::test_additive_sum` | create-project |
| Rate/centered composites (PER/BPM/ORtg/DRtg/USG%/AST%/…) minute-weighted, null-skipping ineligible pools | unit | `...::test_rate_composite_minute_weighted` and `...::test_null_pool_skipped` | create-project |
| Generic filter parsing: valid `(metric, op, value)` predicates parsed; unknown key / non-numeric value / non-filterable col rejected without raising | unit | `tests/unit/test_sl_explorer_filter_parse.py` | create-project |
| Sort-key validation: catalog-driven whitelist; invalid/ungrained keys coerce to default | unit | `tests/unit/test_sl_explorer_sort_validation.py` | create-project |
| Career-grain box totals lossless vs game-log sums after source switch | integration | `tests/integration/test_summer_league_explorer.py::test_career_box_parity_after_source_switch` — seed game logs + season rows, assert explorer career totals == game-log sums | create-project |
| Advanced columns returned + sortable at career & per-competition grain | integration | `...::test_advanced_columns_present_and_sortable_all_grains` (route via HTTPX, assert headers + ordering) | create-project |
| Multi-pool composite is minute-weighted; additive is exact sum (route-level) | integration | `...::test_multipool_composite_vs_additive` — seeded 2-comp player, assert displayed PER==weighted, WS==sum | create-project |
| Metric-threshold filter (HAVING) returns only qualifying rows; combines with facets | integration | `...::test_metric_threshold_filter` and `...::test_metric_filter_with_facets` | create-project |
| Filtered+sorted view round-trips via URL (partial + full page) and CSV export carries advanced cols & all rows | integration | `...::test_advanced_url_roundtrip`, `...::test_csv_export_advanced` | create-project |
| Invalid sort/filter via raw URL never 500 | integration | `...::test_invalid_inputs_graceful` (covers `?sort=bogus`, `?sort=per&grain=per_game`, bad filter predicate) | create-project |
| Pooled composite marked "avg" + eligibility banner reflects N-of-M | integration | `...::test_pooled_avg_marker_and_eligibility_banner` (assert template markers/counts in rendered HTML) | create-project |
| Sub-threshold pools emit blank composites, not blown-up values | integration | `...::test_subthreshold_no_garbage_composites` | create-project |
| Route stays within query budget | integration (perf) | `make perf` for `/stats/summer-league/explorer`; update `tests/integration/perf/budgets.py` only on a conscious bump | create-project |
| Season-table reads/HAVING are index-backed | integration (explain) | `make explain ROUTE=summer-league/explorer` on a prod-like branch | create-project (deferred; see notes) |

## Required Post-Build QA

| Requirement | Verification Path | Evidence |
|---|---|---|
| Advanced metrics visible/sortable with "avg" markers at career grain | e2e / browser (Playwright MCP, no login) | `tests/visual/screenshots/sl-explorer-advanced-career.png` |
| Generic filter builder: AJAX in-place swap, URL sync, back/forward restore | e2e / browser | `sl-explorer-advanced-filters.png` + network 200s, no full reload |
| Single-competition view keeps full composite set without pooled-avg caveat | e2e / browser | `sl-explorer-advanced-single-comp.png` |
| Shared advanced URL renders identically with JS disabled | e2e / browser | screenshot of JS-disabled cold load |
| CSV export opens with advanced columns | manual / browser | downloaded CSV opened, header inspected |
| EXPLAIN on prod-like branch shows Index Scan | manual (post-deploy) | EXPLAIN output captured before prod rollout |

## Ticket Injection Notes

- Ticket: Declarative column catalog + metric taxonomy (P0)
  - Required tests: catalog completeness (every advanced col bucketed), bucket→rollup-fn mapping, sortable/filterable flags.
  - No DB; pure unit.

- Ticket: Unify player subject onto `summer_league_player_seasons` (P1)
  - Required tests: career box parity (lossless vs game-log sums) for a seeded multi-competition player; per-game grain still reads game logs.
  - Required DB assertions: career GP/MIN/PTS/REB/AST equal `SummerLeaguePlayerGameLog` sums; additive WS/VORP equal sum of per-comp season rows.
  - Perf: `make perf` flat or lower vs current budget 9.

- Ticket: Roll-up math (3 buckets) (P1)
  - Required tests: recombinable recomputes from summed components; additive sums; rate composites minute-weighted and null-skip ineligible/sub-threshold pools.

- Ticket: Expose + sort advanced columns at all grains, with labeling (P2)
  - Required tests: advanced headers present at career/per-comp; sort by any advanced key (asc/desc) returns 200 and correct order; pooled composites carry "avg" marker; eligibility banner shows N-of-M; invalid/ungrained sort keys coerce to default.

- Ticket: Generic metric-filter builder (P3)
  - Required tests: parse valid predicates; reject unknown key/non-numeric/non-filterable; HAVING returns only qualifying rows; combines with facets; round-trips through URL (partial + full) and CSV.
  - Required negative cases: raw-URL bad filter never 500s; other valid filters still apply.

- Ticket: Teams-subject advanced (P4, optional/secondary)
  - Required tests: ORtg/DRtg/Pace/NetRtg derived from box-log averages; note plus_minus ~68% populated — assert graceful handling of missing values.

- Deferred (not a build-time blocker): `make explain` index verification — the metrics
  table is not yet on the Neon prod-read branch; capture EXPLAIN before prod rollout and
  add any missing index + Alembic migration in the same change.
