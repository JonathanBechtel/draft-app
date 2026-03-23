# /Stats Page — Combine Data Discovery Hub

## Context

DraftGuru has rich NBA Draft Combine data across three tables (anthro, agility, shooting) but no public-facing way to browse it outside individual player pages. A `/stats` section will make this data discoverable, support year-by-year exploration and metric leaderboards, and create a dense internal link graph for SEO. All data already exists — no new ingestion needed.

**Branch:** `feature/stats-combine-discovery`

---

## Phase 1: Foundation — Service Layer + Landing Page

### 1a. Extract shared formatting helpers

Move formatting functions from `admin_combine_service.py` into a shared utility so both admin and public services can use them.

- **Create** `app/utils/combine_formatters.py` — extract `_format_height_inches()`, `_format_agility_value()`, `_format_shooting_result()` and related helpers
- **Modify** `app/services/admin_combine_service.py` — import from the new utility instead of defining locally

### 1b. Core stats service

- **Create** `app/services/combine_stats_service.py`

Key constructs:
- `METRIC_COLUMN_MAP` — static dict mapping metric keys (e.g. `"wingspan_in"`) to `(table_class, column_name, display_name, unit, category, sort_direction)` for all ~14 anthro+agility metrics
- `get_all_metrics()` → `list[MetricInfo]` — return the full catalog from the map
- `get_metric_info(key)` → `MetricInfo | None` — single lookup
- `get_combine_seasons(db)` → `list[CombineSeasonSummary]` — seasons with combine data + player counts
- `get_leaderboard(db, metric_key, *, year, position, limit, offset)` → `LeaderboardResult` — ranked players for a metric, joining combine table → PlayerMaster → Season, with optional year/position filters, NULL filtering, and correct sort direction (ASC for times, DESC for everything else)

Dataclasses: `MetricInfo`, `LeaderboardEntry`, `LeaderboardResult`, `CombineSeasonSummary`

### 1c. Landing page route + template + navbar

- **Create** `app/routes/stats.py` — new router with `prefix="/stats"`
  - `GET /` — landing page: fetches seasons, 2-3 featured mini-leaderboards (top 5 wingspan, max vert, sprint), metric directory
- **Modify** `app/main.py` — register the new stats router
- **Modify** `app/templates/partials/navbar.html` — add "Stats" link after "Film Room"
- **Create** `app/templates/stats/landing.html` — extends base.html
  - Page header with summary stats (years of data, total players)
  - Featured leaderboard cards (compact top-5 tables)
  - Draft Class year grid (cards linking to `/stats/combine/{year}`)
  - Metric directory grouped by category (Anthro / Agility) linking to leaderboards
- **Create** `app/static/css/stats.css` — shared styles for all stats pages (table, filters, year grid, cards)

### 1 — Testing
- Unit: `tests/unit/test_combine_stats_service.py` — test `get_all_metrics()`, `get_metric_info()`, METRIC_COLUMN_MAP completeness
- Integration: `tests/integration/test_stats_routes.py` — `/stats` returns 200

---

## Phase 2: Full Leaderboard Page

### Route + template

- **Add to** `app/routes/stats.py`:
  - `GET /leaderboards` — query params: `metric` (default `"wingspan_in"`), `year`, `position`, `offset`
  - Validates metric against METRIC_COLUMN_MAP (404 if invalid)
  - Fetches filter options via `get_available_years(db)` and `get_available_positions(db)`
- **Add to** `app/services/combine_stats_service.py`:
  - `get_available_years(db)` → `list[int]`
  - `get_available_positions(db)` → `list[tuple[str, str]]` (code, description)
- **Create** `app/templates/stats/leaderboards.html`
  - Filter bar: `<form method="get">` with `<select>` dropdowns for metric, year, position
  - Results table: Rank, Player (linked), School, Position, Year, Value
  - Pagination (news.html pattern — server-side page math, filter-preserving links)
  - SEO title: `"{Metric Name} Leaderboard — NBA Draft Combine | DraftGuru"`

### 2 — Testing
- Integration: default metric returns 200, invalid metric returns 404, filters preserved in pagination links

---

## Phase 3: Draft Class Explorer

### 3a. Combine index (`/stats/combine`)

- **Add to** `app/routes/stats.py`: `GET /combine` — year grid using `get_combine_seasons(db)`
- **Create** `app/templates/stats/combine-index.html` — year cards with player counts

### 3b. Combine year detail (`/stats/combine/{year}`)

- **Add to** `app/services/combine_stats_service.py`:
  - `get_combine_class(db, year, *, position, sort_by, sort_dir)` → `list[CombinePlayerRow]`
  - LEFT JOINs anthro + agility to PlayerMaster via season, formats all values
  - `CombinePlayerRow` dataclass with all formatted measurement fields
- **Add to** `app/routes/stats.py`: `GET /combine/{year}` — query params: `position`, `sort`, `order`
- **Create** `app/templates/stats/combine-year.html`
  - Wide data table with sortable column headers (server-side sort via query params)
  - Position filter dropdown
  - Player names link to `/players/{slug}`
  - SEO title: `"{Year} NBA Draft Combine Results | DraftGuru"`

### 3 — Testing
- Integration: `/stats/combine` returns 200, `/stats/combine/{valid_year}` returns 200

---

## Phase 4: Metric Deep Dives

- **Add to** `app/services/combine_stats_service.py`:
  - `get_metric_history(db, metric_key)` → `list[MetricYearSummary]` — year-by-year aggregates (avg, min, max, count)
  - `MetricYearSummary` dataclass
- **Add to** `app/routes/stats.py`: `GET /metrics/{metric_key}` — validates key, 404 if invalid
- **Create** `app/templates/stats/metric-detail.html`
  - Metric name + description + unit
  - Year-by-year summary table (Year, Count, Avg, Min, Max)
  - All-time top 10 (reuse `get_leaderboard` with no year, limit=10)
  - Link to full leaderboard
  - SEO title: `"{Metric Name} — NBA Draft Combine History | DraftGuru"`

### 4 — Testing
- Integration: `/stats/metrics/wingspan_in` returns 200, `/stats/metrics/fake` returns 404

---

## Phase 5 (Optional): Position Profiles

- `GET /stats/positions` — index with average measurements per position
- `GET /stats/positions/{code}` — single position archetype
- Service: `get_position_profile(db, code)`, `get_all_position_summaries(db)`
- Templates: `positions-index.html`, `position-detail.html`

Lower priority — implement if Phases 1-4 go smoothly.

---

## Cross-Cutting Details

### SEO
- Add `{% block title %}` and `{% block meta_description %}` to each template (add meta_description block to `base.html` if missing)
- Semantic HTML: `<main>`, `<table>` with `<thead>`/`<tbody>`, `<nav>` for pagination
- Every player name links to their player page; every metric links to its deep dive; every year links to its class page

### CSS
- Single `app/static/css/stats.css` for all stats pages
- BEM classes: `.stats-table`, `.stats-filters`, `.stats-year-grid`, `.stats-metric-card`, `.stats-page-header`
- Follow retro analytics aesthetic (Russo One headings, Azeret Mono for data values, card treatments)

### Key Files to Reuse/Reference
- `app/services/admin_combine_service.py` — formatting helpers, `ANTHRO_METRICS`/`AGILITY_METRICS` constants
- `app/templates/news.html` — best reference for filtered list view with pagination + filter macros
- `app/routes/ui.py` — template rendering pattern, pagination pattern
- `app/schemas/combine_anthro.py`, `combine_agility.py`, `combine_shooting.py` — table definitions

### URL Structure
```
/stats                              → Landing with featured leaderboards + year grid
/stats/leaderboards                 → Full leaderboard tool (filterable)
/stats/leaderboards?metric=X&year=Y → Deep-linked filtered view
/stats/combine                      → Year index
/stats/combine/{year}               → Full class combine results
/stats/metrics/{metric_key}         → Single-metric historical page
/stats/positions/{code}             → Position archetype profile (optional)
```

### Verification (after each phase)
1. `conda run -n draftguru make precommit`
2. `conda run -n draftguru mypy app --ignore-missing-imports`
3. `conda run -n draftguru pytest tests/unit -q`
4. `conda run -n draftguru pytest tests/integration -q` (for route tests)
5. `conda run -n draftguru make visual` for UI verification after template work
