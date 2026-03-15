# Stats Exploration Plan

## Problem

The site has extensive combine stats data (anthropometrics, agility, shooting drills) stored across multiple tables, plus a metrics engine computing percentiles, z-scores, ranks, and player similarity. However, there's no organized starting point for exploring this data — no dedicated stats page, no draft history, no ranking boards. The statistical information lacks a presentable entry point.

## Current Data Assets

**Combine Anthro** (`combine_anthro`): height (w/ and w/o shoes), weight, wingspan, standing reach, hand length, hand width, body fat % — per player/season.

**Combine Agility** (`combine_agility`): lane agility time, shuttle run, 3/4 court sprint, standing vertical, max vertical, bench press reps — per player/season.

**Combine Shooting** (`combine_shooting_results`): 7 drill types (off-dribble, spot-up, 3PT star, 3PT side, midrange star, midrange side, free throw) with FGM/FGA — per player/season.

**Metrics Engine** (`metric_definitions`, `metric_snapshots`, `player_metric_values`): offline-computed percentiles, z-scores, ranks across cohorts (current draft, all-time, by position group).

**Player Similarity** (`player_similarity`): KNN comps across 4 dimensions (anthro, combine, shooting, composite) with similarity scores and rankings.

**Positions** (`positions`): with parent groupings (guard, wing, forward, big) for scoped comparisons.

**Seasons** (`seasons`): multi-year combine data support.

## Current Surface Area

- **Player detail pages**: show percentile bars (currently hardcoded placeholders), comps, news/podcasts/videos
- **News/Podcasts/Film Room**: content feeds with trending players
- **Homepage**: curated prospects, trending, news hero

No way to browse, rank, filter, or compare the statistical data itself.

---

## Proposed Pages

### 1. Combine Stats Landing Page (`/combine-stats`)

**Priority: First** — the entry point for all stats exploration.

A new nav tab alongside News / Podcasts / Film Room. The page shows category cards for each statistical domain:

- **Anthropometrics** — height, weight, wingspan, reach, hand size, body fat
- **Agility & Athleticism** — sprint, agility, verticals, bench press
- **Shooting Drills** — drill-by-drill results across 7 stations

Each card shows:
- Category name and icon
- Number of players with data / seasons covered
- List of metrics included (as chips/tags)
- Latest season indicator
- Link to the category leaderboard (future)

Also includes a season bar showing which draft classes have combine data.

### 2. Category Leaderboards (`/combine-stats/anthropometrics`, etc.)

**Priority: Second** — the drill-down from the landing page.

Sortable, filterable tables showing all players ranked by a specific stat:
- Default sort by the primary metric in the category
- Click any column header to re-sort
- Filter by position group (guard/wing/big), season/draft class
- Percentile bar visualization inline with each row
- Links to player detail pages

### 3. Big Board / Prospect Rankings (`/board`)

**Priority: Third** — the most natural "starting point" for draft fans.

A sortable table of all prospects in a draft class:
- Default sort by consensus rank (when available) or alphabetical
- Columns: rank, name, position, school, key measurables
- Filter by position, school, draft year
- Sort by any stat column
- Fits the retro scoreboard aesthetic — think ticker-style leaderboard

### 4. Player Comparison Tool (`/compare`)

**Priority: Fourth** — already mentioned in the product overview but no route exists.

Head-to-head page for 2-3 players:
- Side-by-side percentile bars
- Stat-by-stat delta highlighting
- Spider/radar chart (optional)
- Shareable as PNG card (per product spec)

### 5. Stats Explorer / Scatter Plot (`/stats/explore`)

**Priority: Fifth** — power-user tool.

Interactive scatter plot where users pick X and Y axes from any metric:
- Dots are players, hover for name, click for detail
- Filter by position/season
- Low effort, high engagement for data-curious users

### 6. Draft History Archive (`/draft/2024`, `/draft/2023`)

**Priority: Sixth** — leverages multi-season data.

Season-indexed pages showing combine results for past draft classes:
- Who measured best, actual draft position
- Connects combine performance to draft outcomes

### 7. Position Profiles (`/positions/guard`)

**Priority: Seventh** — aggregate statistical views.

"What does the average guard prospect look like?" pages:
- Mean/median measurables per position group
- Highlight outliers
- Uses cohort/position-scoped metric snapshots

---

## Implementation Notes

- The `MetricCategory` enum already maps to the three combine categories: `anthropometrics`, `combine_performance`, `shooting`
- The `_CATEGORY_TO_SOURCE` mapping in `metrics_service.py` connects categories to their data sources
- Position scoping via `position_scope_parent` on `MetricSnapshot` supports guard/wing/forward/big filtering
- Season filtering is built into the snapshot system via `season_id`
- The existing `get_player_metrics()` service handles per-player metric retrieval; leaderboards will need a new "all players for a snapshot" query pattern
- `format_metric_value()` in `metrics_service.py` already handles display formatting for all combine stats
