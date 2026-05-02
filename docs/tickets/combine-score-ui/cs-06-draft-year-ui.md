# CS-06: Combine Scores tab and leaderboard table on draft year page

**Placement:** Draft Year Page
**Type:** Frontend (JS + CSS)
**Depends on:** CS-05

## Imperative

Add a "Combine Scores" tab to the draft year page category tabs. When selected, display a full leaderboard table ranking all players by overall combine score with color-coded percentile pills for each category.

## Scope

**JS (`app/static/js/stats-draft-year.js`):**
- When building category tabs, check if `window.DRAFT_YEAR_DATA.combine_scores` is a non-empty array. If so, append a "Combine Scores" tab to the tab bar.
- When the Combine Scores tab is active:
  - Hide the range chart and category leaders sections (they don't apply to composite scores).
  - Show the full results table with combine-score-specific columns.
  - Populate table headers: Player, Overall, Anthro, Athletic, Shooting, Grade.
  - Populate rows from `combine_scores` array, sorted by `overall_rank`.
  - Render each percentile as a `<span class="pctl-pill {tier}">` where tier is derived from the percentile value (elite >= 90, good >= 75, average >= 40, below-average < 40).
  - Player name links to `/players/{slug}`.
  - Support the existing search filter (filter rows by player name or school).
  - Support position filter dropdown.
  - Support cohort selector (vs. Draft Class / vs. All-Time / Position-Adjusted) — this may require fetching additional data or can be a future enhancement. For now, default to "vs. Draft Class" and disable the others if data isn't available.

**CSS (`app/static/css/stats-draft-year.css`):**
- Add `.pctl-pill` styles with tier color variants (elite/good/average/below-average) — these may already exist in `stats.css`; reuse if possible, add to draft-year CSS if not.
- Style the combine scores tab to match the indigo theme when active.
- Ensure the table layout is consistent with the existing full results table styling.

**Design reference:** See Placement 3 in `mockups/draftguru_combine_score.html`.

## Testing

- **Visual test:** `make visual` — screenshot the draft year page with the Combine Scores tab active. Verify table renders with correct columns, pills, and sorting.
- **Integration test:** Hit `/stats/combine/2025`, assert the response includes the JS data payload with `combine_scores`.
- **Manual check:** Verify search filtering works on the combine scores table. Verify player name links navigate correctly. Verify the tab hides range chart / leaders when active and restores them when switching back to another category.

## Definition of Done

- [ ] "Combine Scores" tab appears only when data exists
- [ ] Table renders all players ranked by overall score
- [ ] Percentile pills color-coded by tier
- [ ] Player names link to detail pages
- [ ] Search and position filters work on the combine scores table
- [ ] Switching tabs correctly shows/hides the appropriate sections
- [ ] No regressions to existing category tab behavior
- [ ] `make precommit` passes
- [ ] Visual verification via `make visual`
