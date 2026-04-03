# Combine Score UI — Work Tickets

Surfaces the composite combine score (already computed and stored in `player_metric_values`) across three pages. The service layer (`combine_score_service.py`) is fully implemented but not yet wired to any routes.

**Mockup:** `mockups/draftguru_combine_score.html`

## Ticket Index

### Placement 1 — Player Detail Page
- [CS-01: Wire combine scores into player detail route](cs-01-player-detail-route.md)
- [CS-02: Headline score box component](cs-02-player-detail-headline-box.md)

### Placement 2 — Stats Index Page
- [CS-03: Combine score leaders service + route](cs-03-stats-index-service.md)
- [CS-04: Combine Scores section and leader cards](cs-04-stats-index-ui.md)

### Placement 3 — Draft Year Page
- [CS-05: Year combine scores route data](cs-05-draft-year-route.md)
- [CS-06: Combine Scores tab and leaderboard table](cs-06-draft-year-ui.md)

### Cross-cutting
- [CS-07: Dynamic combine score filtering on player detail](cs-07-dynamic-combine-filters.md)
