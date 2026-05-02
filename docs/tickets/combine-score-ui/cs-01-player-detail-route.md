# CS-01: Wire combine scores into player detail route

**Placement:** Player Detail Page
**Type:** Backend
**Depends on:** None (service layer already exists)

## Imperative

Call `get_player_combine_scores()` from the player detail route in `app/routes/ui.py` and pass the result to the template context. The route currently hardcodes a dummy `percentile_data` dict — the combine score data should be passed as a *separate* context variable (`combine_scores`) alongside the existing percentile data, not replace it.

## Scope

- In the `/players/{slug}` route handler (~line 627 of `app/routes/ui.py`):
  - Import and call `get_player_combine_scores(db, player.id, season_id=season_id)` from `app.services.combine_score_service`.
  - Determine the correct `season_id` from the player's `combine_year` (look up the Season matching that year).
  - Pass the `PlayerCombineScores` dataclass (or `None`) to the template as `combine_scores`.
  - Also pass the `grade_label` function (or pre-compute the grade string) so the template can display it.
- Handle the case where the player has no combine scores gracefully (`combine_scores` is `None`).

## Testing

- **Integration test:** Hit `/players/{slug}` for a player with computed combine scores. Assert `combine_scores` is present in the template context and contains `overall_score` with a valid percentile.
- **Integration test:** Hit `/players/{slug}` for a player without combine scores. Assert the page renders without error and `combine_scores` is `None`.
- **Unit test:** Verify the season lookup logic correctly maps `combine_year` to `season_id`.

## Definition of Done

- [ ] `get_player_combine_scores()` called from route with correct `season_id`
- [ ] `combine_scores` and `combine_grade` available in template context
- [ ] Page renders without error for players with and without scores
- [ ] `make precommit` passes
- [ ] `mypy app --ignore-missing-imports` clean
- [ ] Integration tests pass
