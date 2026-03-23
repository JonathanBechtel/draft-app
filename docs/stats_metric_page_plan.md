# Stats Metric Page — Implementation Plan

## Context

The metric page (`/stats/{metric_key}`) is the core detail view for the /Stats section. It lets users explore any NBA Draft Combine metric via a full leaderboard with filters, summary cards for extremes, and share card export. This plan covers only this page — the stats homepage and draft class pages are separate work items.

**Branch:** `feature/stats-combine-discovery`
**Mockup:** `mockups/draftguru_stats_metric.html`

---

## Product Requirements (from mockup)

### Page Structure
1. **Breadcrumb** — `Stats / {Metric Name}` with link back to `/stats`
2. **Metric Header** — category label (e.g., "Measurements"), metric title (e.g., "Wingspan"), no description text
3. **Summary Cards** — 3-card row: Highest, Lowest, Typical (median)
4. **Filter Bar** — metric dropdown (grouped by category), year dropdown, position dropdown, results count, share buttons
5. **Leaderboard Table** — 25 rows per page with pagination
6. **Back Link** — "Back to All Stats" at bottom

### Summary Cards
- Each card: colored header bar → full-width player image (3:4 aspect) → colored divider line → value + player name + meta
- **Highest** — cyan theme, shows the #1 player for current filters
- **Lowest** — amber theme, shows the last-place player for current filters
- **Typical** — indigo theme, header reads "Typical ({median_value} median)", shows the median player (middle of sorted list, more robust to outliers than mean)
- Cards update when filters change

### Leaderboard Table Columns
1. **Rank** — integer
2. **Player** — 72x96px photo thumbnail + name (linked to player page) + school
3. **Pos** — position code
4. **Year** — draft year
5. **Draft** — pick number + round (e.g., "#6 / Rd 1"), or italic "Undrafted"
6. **Pctl** — color-coded percentile badge (elite: cyan ≥97th, high: green ≥80th, mid: amber ≥50th, low: rose <50th). Recalculates per active filters.
7. **NBA Status** — green "Active" or gray "Out" pill
8. **Value** — the metric value, right-aligned, bold mono

### Table Styling
- Alternating row colors (white / slate-50), no special top-3 styling
- Minimal row padding (images pack tightly)
- Hover highlights rows

### Filters
- **Metric** — `<select>` with `<optgroup>` for Measurements, Athletic Testing, Shooting Drills. Changing metric navigates to `/stats/{new_metric_key}`
- **Year** — "All Years" default + each year with combine data
- **Position** — "All Positions" default + PG/SG/SF/PF/C
- All filters are server-side via query params (form GET submission)

### Share Cards
- Export (download) and Tweet (X) buttons in filter bar, using existing `.export-btn` pattern with same SVG icons as player detail page
- Generates a `stats_leaderboard` share card (1200x630 PNG) showing top 5 for current filters
- Uses existing `ExportModal` / `TweetShare` JS infrastructure

### URL Structure
```
/stats/{metric_key}                          → All-time, all positions
/stats/{metric_key}?year=2024                → Filtered by year
/stats/{metric_key}?position=PG              → Filtered by position
/stats/{metric_key}?year=2024&position=PG    → Both filters
/stats/{metric_key}?offset=25                → Page 2
```

---

## Implementation

### Phase 1: Service Layer

#### 1a. Extract formatting helpers
- **Create** `app/utils/combine_formatters.py`
  - Move `_format_height_inches()`, `_format_agility_value()`, `_format_shooting_result()` from `app/services/admin_combine_service.py`
- **Modify** `app/services/admin_combine_service.py` — import from new location

#### 1b. Stats service
- **Create** `app/services/combine_stats_service.py`

**Static config:**
```python
METRIC_COLUMN_MAP: dict[str, MetricColumnDef]
# Maps metric_key → (table_class, column_name, display_name, unit, category, sort_direction)
# Categories: "measurements", "athletic_testing", "shooting"
# sort_direction: "desc" for most metrics, "asc" for times (lane_agility, shuttle, sprint) and body_fat
```

**Dataclasses:**
```python
@dataclass
class MetricInfo:
    key: str
    display_name: str
    unit: str | None
    category: str
    sort_direction: str  # "asc" or "desc"

@dataclass
class LeaderboardEntry:
    rank: int
    player_id: int
    display_name: str
    slug: str
    school: str | None
    position: str | None
    draft_year: int | None
    draft_round: int | None
    draft_pick: int | None
    is_active_nba: bool
    raw_value: float
    formatted_value: str
    percentile: float | None  # 0-100, computed from current filter population

@dataclass
class LeaderboardResult:
    entries: list[LeaderboardEntry]
    total: int
    metric: MetricInfo
    # Summary card data
    highest: LeaderboardEntry | None
    lowest: LeaderboardEntry | None
    median_value: float | None
    typical: LeaderboardEntry | None  # the median player (middle of sorted list)

@dataclass
class CombineSeasonSummary:
    season_id: int
    season_code: str
    start_year: int
    player_count: int
```

**Service functions:**
```python
def get_all_metrics() -> list[MetricInfo]
def get_metric_info(key: str) -> MetricInfo | None
def get_metrics_grouped() -> dict[str, list[MetricInfo]]  # grouped by category for dropdown optgroups

async def get_leaderboard(
    db: AsyncSession,
    metric_key: str,
    *,
    year: int | None = None,
    position: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> LeaderboardResult
# - Joins combine table → PlayerMaster (name, slug, school, draft_pick, draft_round, draft_year)
#   → PlayerStatus (is_active_nba, position) → Season (for year filter)
# - Filters: non-null metric values, optional year, optional position
# - Orders by value (ASC for times, DESC for rest)
# - Computes rank via ROW_NUMBER or enumeration
# - Computes percentile within filtered population
# - Fetches highest, lowest, median, and typical (the median player) for summary cards
# - Median: sort filtered population by value, pick the middle entry (offset = total // 2, limit = 1)
# - Formats values using combine_formatters

async def get_available_years(db: AsyncSession) -> list[int]
async def get_available_positions(db: AsyncSession) -> list[tuple[str, str]]  # (code, description)
```

**Key query patterns to reuse:**
- `admin_combine_service.py` — combine table queries, formatting helpers
- `metrics_service.py` — percentile computation pattern from `_metric_population_size()`
- `player_service.py` — PlayerMaster → PlayerStatus join pattern

### Phase 2: Route + Template

#### Route
- **Create** `app/routes/stats.py` — new router `prefix="/stats"`
- **Modify** `app/main.py` — register stats router
- **Modify** `app/templates/partials/navbar.html` — add "Stats" link after "Film Room"

```python
LEADERBOARD_PAGE_LIMIT = 25

@router.get("/{metric_key}", response_class=HTMLResponse)
async def metric_leaderboard(
    request: Request,
    metric_key: str,
    year: int | None = Query(default=None),
    position: str | None = Query(default=None),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_session),
):
    metric = get_metric_info(metric_key)
    if not metric:
        raise HTTPException(404)

    result = await get_leaderboard(db, metric_key, year=year, position=position, limit=LEADERBOARD_PAGE_LIMIT, offset=offset)
    years = await get_available_years(db)
    positions = await get_available_positions(db)
    metrics_grouped = get_metrics_grouped()

    # Transform entries to list[dict] for template
    # Pass: result, metric, years, positions, metrics_grouped, offset, limit, filters
```

#### Template: `app/templates/stats/metric.html`
- Extends `base.html`
- `{% block title %}` — `"{Metric Name} — NBA Draft Combine | DraftGuru"`
- `{% block meta_description %}` — dynamic SEO description
- `{% block extra_css %}` — `stats.css`
- Breadcrumb, header, summary cards, filter bar, table, pagination, back link
- Filter bar uses `<form method="get">` preserving all params
- Pagination uses Jinja macros (news.html pattern) to preserve filters in page links
- Player images use existing fallback chain (`photo_url` → `photo_url_default` → `photo_url_placeholder`)
- Share buttons wired to `ExportModal.export('stats_leaderboard', [...], context)` and `TweetShare.share({...})`

#### CSS: `app/static/css/stats.css`
All styles from the mockup, organized by component:
- `.breadcrumb`, `.breadcrumb-nav`
- `.metric-header`, `.metric-header__title`, `.metric-header__category`
- `.stat-summary-row`, `.stat-summary-card` (with `--high`, `--low`, `--avg` variants)
- `.filters-bar`, `.filter-select`
- `.leaderboard-card`, `.leaderboard-table` (thead, tbody, alternating rows)
- `.cell-rank`, `.cell-player`, `.cell-draft`, `.cell-percentile`, `.cell-status`, `.cell-value`
- `.nba-badge` (with `--active`, `--inactive`)
- `.pagination`
- Responsive breakpoints

### Phase 3: Share Card Integration

- **Add** `"stats_leaderboard"` component to `app/services/share_cards/constants.py`
- **Create** `app/services/share_cards/model_builders.py` — `build_stats_leaderboard_model()`
  - Fetches top 5 for current metric/filters
  - Embeds player photos as base64
  - Returns `StatsLeaderboardRenderModel` with title, context line, 5 ranked rows
- **Create** render model in `app/services/share_cards/render_models.py`:
  ```python
  @dataclass
  class StatsLeaderboardRow:
      rank: int
      name: str
      subtitle: str  # "School · Position · Year"
      value: str
      photo_data_uri: str | None

  @dataclass
  class StatsLeaderboardRenderModel:
      title: str  # "Wingspan"
      context: str  # "All-Time · All Positions" or "2024 · Guards"
      rows: list[StatsLeaderboardRow]  # 5 entries
  ```
- **Create** SVG template `app/templates/export_svg/stats_leaderboard.svg`
  - 2400x1260 canvas (renders at 1200x630)
  - Header with metric name + filter context
  - 5 rows with player photos, rank, name, school, value
  - DraftGuru watermark footer
- **Register** in `export_service.py` component dispatch
- **Add** JS context gathering in `{% block extra_js %}`:
  ```javascript
  function exportStatsLeaderboard() {
      const playerIds = window.LEADERBOARD_PLAYER_IDS.slice(0, 5);
      const context = { metric: '...', year: '...', position: '...' };
      ExportModal.export('stats_leaderboard', playerIds, context);
  }
  ```

---

## Key Files

### Create
- `app/utils/combine_formatters.py`
- `app/services/combine_stats_service.py`
- `app/routes/stats.py`
- `app/templates/stats/metric.html`
- `app/static/css/stats.css`
- `app/templates/export_svg/stats_leaderboard.svg`
- `tests/unit/test_combine_stats_service.py`
- `tests/integration/test_stats_routes.py`

### Modify
- `app/services/admin_combine_service.py` — extract formatters
- `app/main.py` — register stats router
- `app/templates/partials/navbar.html` — add Stats nav link
- `app/services/share_cards/constants.py` — add stats_leaderboard component
- `app/services/share_cards/export_service.py` — register new component
- `app/services/share_cards/render_models.py` — add StatsLeaderboardRenderModel
- `app/services/share_cards/model_builders.py` — add build_stats_leaderboard_model()
- `tests/integration/conftest.py` — ensure combine schema imports for table creation

### Reference (read-only)
- `mockups/draftguru_stats_metric.html` — the mockup (source of truth for UI)
- `app/services/admin_combine_service.py` — combine query patterns, formatting helpers
- `app/services/metrics_service.py` — percentile computation patterns
- `app/templates/news.html` — filter macro + pagination pattern
- `app/routes/ui.py` — template rendering pattern
- `app/static/css/export-modal.css` — `.export-btn` styles (already globally loaded)
- `app/templates/player-detail.html` — share button SVG icons + JS wiring

---

## Testing

### Unit Tests: `tests/unit/test_combine_stats_service.py`

**Static config tests:**
- `test_get_all_metrics_returns_all_entries` — verify count matches METRIC_COLUMN_MAP, all have required fields
- `test_get_metric_info_valid_key` — returns correct MetricInfo for known key (e.g., `wingspan_in`)
- `test_get_metric_info_invalid_key` — returns None for unknown key
- `test_get_metrics_grouped_has_all_categories` — returns dict with "measurements", "athletic_testing", "shooting" keys
- `test_metric_column_map_sort_directions` — verify times (lane_agility, shuttle_run, three_quarter_sprint, body_fat_pct) are "asc", rest are "desc"
- `test_metric_column_map_tables_valid` — every entry references a real SQLModel table class with a real column name

**Formatting tests (combine_formatters):**
- `test_format_height_inches_whole` — e.g., 72.0 → `6'0"`
- `test_format_height_inches_fractional` — e.g., 94.0 → `7'10"`
- `test_format_height_inches_quarter` — e.g., 88.25 → `7'4.25"`
- `test_format_height_inches_none` — None → None or empty string
- `test_format_agility_value` — e.g., 3.04 → `"3.04s"`
- `test_format_agility_value_none` — None → None or empty string

### Integration Tests: `tests/integration/test_stats_routes.py`

**Setup fixtures** (following existing patterns from `test_player_metrics.py`):
- Factory helpers: `_create_position()`, `_create_player()`, `_create_season()`, `_create_combine_anthro()`, `_create_combine_agility()`, `_create_player_status()`
- Seed 5+ players with combine anthro data (varying wingspan values) across 2 seasons, with PlayerStatus (is_active_nba, position), and draft info on PlayerMaster

**Route tests:**
- `test_metric_page_returns_200` — `GET /stats/wingspan_in` returns 200 with seeded data
- `test_metric_page_invalid_key_returns_404` — `GET /stats/fake_metric` returns 404
- `test_metric_page_contains_player_names` — response HTML includes seeded player display names
- `test_metric_page_contains_table_headers` — response HTML has Rank, Player, Pos, Year, Draft, Pctl, NBA Status, Wingspan
- `test_metric_page_year_filter` — `GET /stats/wingspan_in?year=2024` only shows players from 2024 season
- `test_metric_page_position_filter` — `GET /stats/wingspan_in?position=C` only shows centers
- `test_metric_page_combined_filters` — `GET /stats/wingspan_in?year=2024&position=C` applies both
- `test_metric_page_pagination` — `GET /stats/wingspan_in?offset=25` returns page 2 (or empty if <25 seeded)
- `test_metric_page_summary_cards_present` — response HTML contains "Highest", "Lowest", "Typical" text
- `test_metric_page_breadcrumb` — response HTML contains breadcrumb with link to `/stats`
- `test_metric_page_filter_dropdowns` — response HTML contains metric, year, and position `<select>` elements
- `test_metric_page_share_buttons` — response HTML contains export-btn elements

**Service tests (via DB, in integration):**
- `test_get_leaderboard_returns_sorted_desc` — wingspan results ordered highest first
- `test_get_leaderboard_returns_sorted_asc_for_times` — lane_agility results ordered lowest first
- `test_get_leaderboard_filters_nulls` — players with NULL wingspan excluded
- `test_get_leaderboard_highest_lowest_typical` — result has correct highest, lowest, and typical (median) entries
- `test_get_leaderboard_percentile_computation` — top player has percentile ~99-100, bottom has ~0-1
- `test_get_leaderboard_draft_info_populated` — entries include draft_pick, draft_round from PlayerMaster
- `test_get_leaderboard_nba_status_populated` — entries include is_active_nba from PlayerStatus
- `test_get_available_years` — returns years with combine data, sorted descending
- `test_get_available_positions` — returns position codes that have combine data

---

## Verification

After each phase:
1. `conda run -n draftguru make precommit`
2. `conda run -n draftguru mypy app --ignore-missing-imports`
3. `conda run -n draftguru pytest tests/unit -q`
4. `conda run -n draftguru pytest tests/integration -q`
5. `conda run -n draftguru make visual` for UI verification
