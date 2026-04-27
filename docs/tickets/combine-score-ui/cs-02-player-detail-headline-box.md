# CS-02: Headline score box component on player detail page

**Placement:** Player Detail Page
**Type:** Frontend (template + CSS + JS)
**Depends on:** CS-01

## Imperative

Add a headline combine score box at the top of the Performance section in `player-detail.html`, above the existing category tabs. The box shows the overall percentile as a donut ring, the grade label, a compact category breakdown, and the player's rank. When `combine_scores` is `None`, the box is not rendered.

## Scope

**Template (`app/templates/player-detail.html`):**
- Inside the `{% if player.combine_year %}` Performance section, add the headline box markup *before* the `perf-tabs` div.
- Wrap in `{% if combine_scores and combine_scores.overall_score %}`.
- Render: donut ring (SVG), overall percentile number, grade badge, category breakdown dots (Anthro / Athletic / Shooting percentiles), rank block.
- Category breakdown should iterate over `combine_scores.category_scores` and display each available category with its color-coded dot.

**CSS (`app/static/css/player-detail.css` or inline in block):**
- Add styles for `.combine-headline` and child elements.
- Follow the patterns from the mockup: indigo outline, dot-grid background, pixel corners.
- Donut ring uses SVG `stroke-dasharray` / `stroke-dashoffset` calculated from the percentile.

**JS (`app/static/js/player-detail.js`):**
- Animate the donut ring fill on page load (transition the `stroke-dashoffset`).
- The ring color class (elite/above-avg/avg/below-avg) should be set based on the percentile value, matching the `grade_label` thresholds from `combine_score_service.py`.

**Design reference:** See Placement 1 in `mockups/draftguru_combine_score.html`.

## Testing

- **Visual test:** `make visual` — screenshot the player detail page for a player with combine scores. Verify the headline box appears above the tabs with correct layout.
- **Visual test:** Verify the box does not appear for players without combine scores.
- **Integration test:** Assert the headline box container is present in the HTML response for a player with scores, absent for one without.

## Definition of Done

- [ ] Headline score box renders with real data from `combine_scores` context
- [ ] Donut ring, grade badge, category dots, and rank all display correctly
- [ ] Box hidden when player has no combine scores
- [ ] Styles match mockup and align with existing design system
- [ ] No layout shifts or breakage to existing Performance section tabs/bars
- [ ] `make precommit` passes
- [ ] Visual verification via `make visual`
