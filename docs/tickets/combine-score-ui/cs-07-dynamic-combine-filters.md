# CS-07: Dynamic combine score filtering on player detail page

**Placement:** Player Detail Page
**Type:** Backend (API) + Frontend (JS)
**Depends on:** CS-01, CS-02

## Imperative

Make the combine score headline box respond to the existing cohort dropdown and position-adjusted toggle. When the user switches from "Current Draft Class" to "All-Time NBA" or toggles position adjustment, the headline box should update its percentile, grade, rank, category breakdown, and donut ring to reflect the selected scope. Currently the box is server-rendered with a single static value (current_draft, baseline position scope).

## Background

The existing `PerformanceModule` already handles this pattern for percentile bars: on filter change it calls `GET /api/players/{slug}/metrics?cohort=...&category=...&position_adjusted=...` and re-renders. The combine score headline needs the same reactivity.

All required data exists in the database — combine_score snapshots are promoted for every cohort (current_draft, all_time_draft, current_nba, all_time_nba) and every position scope (baseline + guard/wing/forward/big).

### Cohort mapping (from PerformanceModule.mapCohort):
- "Current Draft Class" → `current_draft` (per-season snapshot, uses player's combine_year)
- "Historical Prospects" → `all_time_draft` (global snapshot, season_id=None)
- "Active NBA Players" → `current_nba` (global snapshot, season_id=None)
- "All-Time NBA" → `all_time_nba` (global snapshot, season_id=None)

### Position-adjusted mapping:
- Checked → use player's `position_scope_parent` (guard/wing/forward/big)
- Unchecked → `position_scope_parent=None` (baseline, all positions)

## Scope

### Option A: Add combine_score to existing metrics API response (recommended)

**API (`app/routes/players.py`, `/api/players/{slug}/metrics` handler):**
- After fetching category metrics, also call `get_player_combine_scores()` with the same cohort and position scope.
- For `current_draft` cohort, pass the player's `season_id` (already resolved in the service). For global cohorts (`all_time_draft`, `current_nba`, `all_time_nba`), pass `season_id=None`.
- Add a `combine_score` key to the `PlayerMetricsResponse` model containing: `overall_percentile`, `overall_rank`, `grade`, `population_size`, and `categories` (list of `{key, label, percentile, color}`).
- If no combine scores exist for the scope, `combine_score` is `null`.

**Response model (`app/models/player_metrics.py` or equivalent):**
- Add `combine_score: Optional[CombineScorePayload]` to `PlayerMetricsResponse`.
- Define `CombineScorePayload` with: `overall_percentile: float`, `overall_rank: int`, `grade: str`, `population_size: Optional[int]`, `categories: list[CombineScoreCategoryPayload]`.
- Define `CombineScoreCategoryPayload` with: `key: str`, `label: str`, `percentile: float`, `color: str`.

**JS (`app/static/js/player-detail.js`):**
- In `PerformanceModule.fetchAndRender()`, after receiving the API response, call `CombineScoreModule.update(data.combine_score)`.
- `CombineScoreModule.update(data)`:
  - If `data` is null, hide `.combine-headline`.
  - Otherwise, update: `.combine-headline__number` (percentile), `.ring-fill` (stroke-dashoffset + tier class), `.combine-headline__grade-badge` (text + CSS class), `.combine-headline__grade-context` (cohort label), `.combine-headline__rank-number` (rank), `.combine-headline__rank-total` (population), and category dots (rebuild from `data.categories`).
  - Re-trigger the donut ring animation on each update.
- Remove the server-side `combine_scores`/`combine_grade`/`combine_population` from the route context (the initial render can use the first API response, or keep the server render as a fast initial paint and let JS overwrite on filter change).

### Option B: Separate API endpoint (alternative)

- Create `GET /api/players/{slug}/combine-score?cohort=...&position_adjusted=...`
- `CombineScoreModule` listens to the same cohort/position-adjusted events independently and makes its own fetch.
- Pro: clean separation. Con: extra request per filter change.

## Existing code to reuse

- `get_player_combine_scores(db, player_id, cohort, season_id, position_scope_parent)` in `app/services/combine_score_service.py` — already accepts all needed parameters.
- `grade_label(percentile)` in the same service — maps percentile to grade string.
- `PerformanceModule.mapCohort()` in `app/static/js/player-detail.js` (line 215) — maps UI cohort values to API cohort strings.
- `PerformanceModule.setupEventListeners()` (line 181) — existing listeners for cohort and position-adjusted changes.
- Snapshot population_size is on `MetricSnapshot.population_size` (needs a query or can be included in the service return).

## Testing

- **Integration test:** Hit `/api/players/{slug}/metrics?cohort=current_draft&category=anthropometrics&position_adjusted=true` for a player with combine scores. Assert `combine_score` is present in the JSON response with `overall_percentile`, `grade`, `rank`, and `categories`.
- **Integration test:** Same request with `cohort=all_time_nba`. Assert `combine_score` reflects the global cohort (different percentile/rank than current_draft).
- **Integration test:** Same request with `position_adjusted=false`. Assert the values differ from position-adjusted.
- **Integration test:** Player without combine data. Assert `combine_score` is `null`.
- **Visual test:** Toggle cohort dropdown on a player page. Verify the headline box updates (donut ring re-animates, numbers change, grade badge updates).
- **Visual test:** Toggle position-adjusted checkbox. Verify headline box updates.

## Definition of Done

- [ ] Combine score data included in metrics API response (or separate endpoint)
- [ ] Headline box updates dynamically when cohort dropdown changes
- [ ] Headline box updates dynamically when position-adjusted toggle changes
- [ ] Donut ring re-animates on each update with correct tier color
- [ ] Grade badge, rank, population, and category dots all update
- [ ] Box hides gracefully if no combine score exists for the selected scope
- [ ] Cohort label in the box updates (e.g., "vs. All-Time NBA" instead of "vs. 2025 Draft Class")
- [ ] `make precommit` passes
- [ ] `mypy app --ignore-missing-imports` clean
- [ ] Integration tests pass
- [ ] Visual verification via `make visual`
