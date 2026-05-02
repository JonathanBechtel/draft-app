# CS-04: Combine Scores section and leader cards on stats index

**Placement:** Stats Index Page
**Type:** Frontend (template + CSS)
**Depends on:** CS-03

## Imperative

Add a "Combine Scores" section to the stats homepage between Shooting Drills and Draft Classes. Include a new tab in the sticky section nav and render leader cards using the indigo theme, following the existing `leader_card` macro pattern.

## Scope

**Template (`app/templates/stats/index.html`):**
- Add a new sticky nav tab: `Combine Scores` with trophy icon, linking to `#combine-scores`.
- Add a new section (`id="combine-scores"`) between `#shooting-drills` and `#draft-classes`, with a section divider above it.
- Section header: "Combine Scores" with indigo border-left.
- Subtitle: "Composite rankings blending measurements, athleticism, and shooting".
- Create a `combine_leader_card` macro (or adapt `leader_card`) for the indigo theme. Key differences from existing leader cards:
  - Unit is "pctl" instead of a measurement unit.
  - Overall card includes a mini category breakdown (colored dots with sub-scores).
  - "View All" links to `/stats/combine/{year}` (the draft year page with combine scores tab).
- Render cards in a `leaderboard-grid` (3-column): Overall, Anthro, Athletic. Optionally include Shooting as a 4th card if data exists.

**CSS (`app/static/css/stats.css`):**
- Add `.stat-leader-card--indigo` variant following the existing cyan/amber/rose pattern: indigo outline, gradient header (`#e0e7ff` to `#c7d2fe`), indigo border-bottom, indigo pixel corners.
- Add `.stats-nav-tab--scores` for the new sticky nav tab with indigo hover/active color.
- Add `.featured-score-breakdown` styles for the mini category dots in the overall leader card.

**Design reference:** See Placement 2 in `mockups/draftguru_combine_score.html`.

## Testing

- **Visual test:** `make visual` — screenshot the stats homepage. Verify the new section appears with indigo-themed cards populated with real data.
- **Integration test:** Hit `/stats/` and assert the combine scores section markup is present in the response.
- **Manual check:** Verify "View All" links route correctly to the draft year page.

## Definition of Done

- [ ] "Combine Scores" tab in sticky nav, correctly anchored
- [ ] Indigo-themed leader cards render with real data from `combine_score_leaders`
- [ ] Overall card shows category breakdown dots
- [ ] Cards follow existing leader card layout patterns
- [ ] Section hidden gracefully if no combine score data exists
- [ ] `make precommit` passes
- [ ] Visual verification via `make visual`
