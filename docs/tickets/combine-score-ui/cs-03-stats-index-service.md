# CS-03: Combine score leaders service + route data

**Placement:** Stats Index Page
**Type:** Backend (service + route)
**Depends on:** None (service layer already exists)

## Imperative

Add a function to fetch combine score leaders (top N players by overall, anthro, athletic, and shooting scores) for the most recent draft year, and wire it into the stats homepage route so the template has the data it needs for leader cards.

## Scope

**Service (`app/services/combine_score_service.py` or `combine_stats_service.py`):**
- Add a `get_combine_score_leaders()` function that returns a dict keyed by score type (`combine_score_overall`, `combine_score_anthropometrics`, `combine_score_athletic`, `combine_score_shooting`), each containing a list of top players (default 5) with: `player_id`, `display_name`, `slug`, `photo_url`, `position`, `school`, `percentile`, `rank`, and `category_scores` breakdown (for the overall leader card).
- Reuse `get_year_combine_scores()` internally, or query `PlayerMetricValue` directly for efficiency.
- Use the most recent season that has a current `combine_score` snapshot.

**Route (`app/routes/stats.py`, `/stats/` handler):**
- Call `get_combine_score_leaders()` and pass the result as `combine_score_leaders` in the template context.
- Also pass a display config dict mapping each score type to its icon, superlative label ("Top Overall Score", "Best Anthro Score", etc.), and color.

## Testing

- **Integration test:** Hit `/stats/` and assert `combine_score_leaders` is in the context with the expected keys and at least one entry per score type (assuming dev DB has computed scores).
- **Unit test:** Test `get_combine_score_leaders()` returns correctly shaped data, handles missing snapshot gracefully (returns empty dict), and respects the `limit` parameter.

## Definition of Done

- [ ] `get_combine_score_leaders()` implemented and returns leader lists for all 4 score types
- [ ] Stats homepage route passes `combine_score_leaders` to template
- [ ] Handles missing/empty data without error
- [ ] `make precommit` passes
- [ ] `mypy app --ignore-missing-imports` clean
- [ ] Integration and unit tests pass
