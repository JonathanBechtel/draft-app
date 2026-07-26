# Summer League Explorer — Advanced Metrics QA Checklist

**Sources:**
- Tech spec: `docs/plans/summer-league-explorer-advanced-metrics.md`

**Sibling artifact:** test plan at `summer-league-explorer-advanced-metrics-test-plan.md`

This checklist defines product-level behaviors QA should verify before considering
"advanced metrics first-class in the Summer League explorer" complete. The explorer
lives at `/stats/summer-league/explorer`. Explorer is public — no login required for
the feature itself.

## Core User Behaviors

- A user can see advanced metrics in the player results at **every** applicable grain, not just single-competition.
  - Verify: open the explorer (player subject), career grain, no venue/year filter; inspect the results table header row.
  - Expected: advanced columns (TS%, eFG%, PER, BPM, ORtg, DRtg, USG%, AST%, TRB%, WS, VORP, GameScore, …) are present alongside box columns.
  - Evidence: rendered table header + a populated data row; screenshot.

- A user can sort by any advanced metric at any grain.
  - Verify: click the PER column header at career grain; then click again to reverse.
  - Expected: rows reorder by PER descending, then ascending; URL reflects `sort=per` + direction; no 500.
  - Evidence: top-row PER value is the max (then min); browser network request returns 200.

- A user can sort by a metric and then change grain without an unexplained reset.
  - Verify: sort by VORP at career grain, then switch to per-competition grain.
  - Expected: sort persists where the column still applies; if a column is unavailable at the new grain the UI explains the change rather than silently coercing.
  - Evidence: visible sort indicator + any "sort reset because …" note.

- A user can filter players by an advanced-metric threshold using the generic filter builder.
  - Verify: add a filter row `PER ≥ 15`, submit; then add a second row `USG% ≥ 20`.
  - Expected: only players meeting **all** active thresholds remain; result count drops accordingly; filters appear in the shareable URL.
  - Evidence: every visible row satisfies the thresholds; result count badge; URL query string.

- A user can combine advanced-metric filters with existing facet filters.
  - Verify: set draft_class + venue + `min_min`, then add `BPM ≥ 2.0`.
  - Expected: results respect both the facets and the metric threshold simultaneously.
  - Evidence: spot-check a row against all active constraints.

- A user can share/reload a filtered+sorted advanced view and get the same result.
  - Verify: copy the URL after applying advanced sort + 2 metric filters; open in a fresh tab.
  - Expected: identical filters, sort, and rows render on cold load (works with JS disabled too).
  - Evidence: side-by-side result parity; disable-JS reload still renders server-side.

- A user can export the advanced columns to CSV.
  - Verify: apply an advanced sort/filter, click CSV export.
  - Expected: downloaded CSV includes the advanced columns currently shown, all matching rows (not just the current page), values matching the table.
  - Evidence: CSV header + row count vs. on-screen total.

## Persistence And Data Integrity

- Career-grain box totals are **lossless** after the data-source switch to `summer_league_player_seasons`.
  - Verify: for a sample of players, compare career GP/MIN/PTS/REB/AST from the new explorer against the pre-change game-log-sum values (or against `SummerLeaguePlayerGameLog` sums directly).
  - Expected: exact equality for every box total and GP.
  - Evidence: DB query comparison; documented sample.

- Additive shares roll up as exact sums across a multi-pool selection.
  - Verify: pick a player with ≥2 adv-eligible competitions; sum their per-competition WS and VORP manually; compare to the career-grain explorer value.
  - Expected: career WS/VORP == sum of per-competition WS/VORP (within float tolerance).
  - Evidence: arithmetic against `summer_league_player_seasons` rows.

- Exact-recombinable metrics are recomputed from summed components, not averaged.
  - Verify: for a multi-competition player, confirm career TS% == total PTS / (2 × (total FGA + 0.44 × total FTA)) from summed box totals.
  - Expected: matches the recombined formula, not a simple mean of per-comp TS%.
  - Evidence: hand calculation vs. displayed value.

- Pooled rate/centered composites are minute-weighted averages, null-skipping ineligible pools.
  - Verify: for a player with one adv-eligible and one ineligible competition, confirm career PER == the eligible pool's PER (the ineligible/NULL pool is excluded from the weighting).
  - Expected: ineligible-pool composites (NULL) do not drag the average to 0 or NaN.
  - Evidence: computed minute-weighted value vs. displayed.

## Scope, Auth, And Safety

- Hand-typed/invalid sort keys never 500.
  - Verify: load `?subject=players&grain=career&sort=bogus_metric` and `?sort=per&grain=per_game`.
  - Expected: graceful fallback to a valid default sort; page renders 200.
  - Evidence: response code + sort indicator.

- Invalid/unknown filter inputs are rejected safely.
  - Verify: submit a filter with an unknown metric key, a non-numeric value, and an out-of-catalog column via raw URL params.
  - Expected: the bad predicate is ignored or surfaced as a validation message; no 500; no SQL error; other valid filters still apply.
  - Evidence: response 200; logs show no unhandled exception.

- Pooled composites are never presented as exact.
  - Verify: at multi-pool grain, inspect any rate/centered composite cell (PER/BPM/ORtg/DRtg/USG%…).
  - Expected: a visible "avg" marker and a tooltip naming the per-pool-calibration caveat.
  - Evidence: rendered marker + tooltip text; screenshot.

- Composite eligibility is communicated honestly.
  - Verify: select a multi-competition pool where only some competitions are adv-eligible.
  - Expected: banner states "N of M competitions in this pool qualify for composites" (generalized from the single-comp `adv_eligible` warning).
  - Evidence: banner text matches actual eligible/total counts.

## Operational Behavior

- The route stays within its query budget.
  - Verify: run `make perf` for `/stats/summer-league/explorer`.
  - Expected: query count ≤ budget (currently 9; unifying on the season table should be flat or lower). Any conscious bump is reflected in `tests/integration/perf/budgets.py`.
  - Evidence: `make perf` output.

- New/changed queries are index-backed.
  - Verify: `make explain ROUTE=...` against a prod-like Neon branch on the season-table reads + HAVING filters.
  - Expected: Index Scan on `summer_league_player_seasons` (by competition_id / year / venue_slug / player_id), not a Seq Scan on a large table.
  - Evidence: EXPLAIN output. **Blocker note:** the metrics table is not yet on the Neon prod-read branch — this check is deferred until the metrics migration is deployed there; capture it before prod rollout.

- Percentage columns render with the correct convention.
  - Verify: a TS%/USG% value in the explorer matches the player page for the same player-competition.
  - Expected: stored-as-percentage values (e.g., 60.6, not 0.606) render identically and are not double-scaled.
  - Evidence: cross-surface value parity.

- Sub-threshold (below 40-min floor) player-competitions don't emit garbage composites.
  - Verify: filter to a pool including known small-sample players; inspect composite cells.
  - Expected: composites for sub-`DISPLAY_MIN_MINUTES` pools are blank/excluded, not PER 70+; box stats still present.
  - Evidence: a known small-sample row shows em-dash composites + real box stats.

## Final Browser QA

Run `make dev` (server on `http://localhost:8000`), then drive the live explorer
(no login needed for this page) per `docs/plans/ai-orchestrator-ticket-spec.md`:

- Player subject, career grain: advanced columns visible, sortable, with "avg" markers on composites; capture `sl-explorer-advanced-career.png`.
- Apply 2 metric filters via the builder; confirm AJAX in-place swap (no full reload), URL sync, and back/forward (`popstate`) restore the prior view; capture `sl-explorer-advanced-filters.png`.
- Per-competition single-comp view still shows the full composite set without the pooled-avg caveat; capture `sl-explorer-advanced-single-comp.png`.
- JS-disabled reload of a shared advanced URL renders server-side identically.
- CSV export of an advanced view downloads and opens with the advanced columns.
- Save captures under `tests/visual/screenshots/`.

## Completion Bar

The feature is product-complete when QA can demonstrate:
1. Advanced metrics are visible, sortable, and filterable for the player subject at career, per-competition, and per-game grains (where the metric is defined).
2. Multi-pool composites are minute-weighted, clearly labeled "avg," and never presented as exact; additive shares sum exactly; recombinable metrics recompute from box totals.
3. Career-grain box totals are lossless versus the prior game-log-sum implementation.
4. The generic metric-filter builder filters by any catalog metric, validates input, and round-trips through the shareable URL and CSV export.
5. The route stays within its query budget and its season-table reads/HAVING filters are index-backed (EXPLAIN captured before prod rollout).
6. Invalid sort keys and filter inputs degrade gracefully (no 500s).
