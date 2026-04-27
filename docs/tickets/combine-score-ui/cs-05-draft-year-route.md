# CS-05: Year combine scores route data

**Placement:** Draft Year Page
**Type:** Backend (route)
**Depends on:** None (service layer already exists)

## Imperative

Wire `get_year_combine_scores()` into the draft year route so the page has combine score data available for a new "Combine Scores" tab in the frontend.

## Scope

**Route (`app/routes/stats.py`, `/stats/combine/{year}` handler):**
- Call `get_year_combine_scores(db, season_id)` from `combine_score_service`.
- Serialize the result list into the `draft_year_json` payload that gets injected as `window.DRAFT_YEAR_DATA`. Add a `combine_scores` key containing an array of objects, each with:
  - `player_id`, `player_name`, `player_slug`, `position`, `school`
  - `overall_percentile`, `overall_rank`
  - `overall_grade` (computed via `grade_label()`)
  - `anthro_percentile`, `athletic_percentile`, `shooting_percentile` (nullable)
- Determine the season_id from the `{year}` path parameter (existing logic already does this).
- If no combine scores exist for the year, `combine_scores` should be an empty array (the JS will hide the tab).

**Serialization:**
- The existing route builds `draft_year_json` as a dict. Add the `combine_scores` key to this dict.
- Map `PlayerCombineScores` dataclasses to plain dicts suitable for JSON serialization.
- Include player position and school by joining against `PlayerMaster` (the service already fetches `player_name` and `player_slug`; position/school may need to be added).

## Testing

- **Integration test:** Hit `/stats/combine/2025` and parse the embedded `DRAFT_YEAR_DATA` JSON. Assert `combine_scores` is a non-empty array with expected fields.
- **Integration test:** Hit a year with no combine scores. Assert `combine_scores` is an empty array and the page renders without error.
- **Unit test:** Test the serialization logic maps `PlayerCombineScores` correctly, including nullable category scores.

## Definition of Done

- [ ] `get_year_combine_scores()` called from draft year route
- [ ] `combine_scores` array included in `window.DRAFT_YEAR_DATA` JSON
- [ ] Each entry includes overall + category percentiles, rank, grade, and player metadata
- [ ] Empty array when no data exists (no errors)
- [ ] `make precommit` passes
- [ ] `mypy app --ignore-missing-imports` clean
- [ ] Integration tests pass
