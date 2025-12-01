# Player Profile Implementation

This document summarizes the work completed to connect the player profile page to real database data.

## Overview

The player detail page (`/players/{slug}`) now displays real biographical data from the database instead of hardcoded placeholder values. The Draft Analytics Scoreboard is hidden until data sources are available.

## Data Sources

Player profile data is assembled from multiple tables:

| Field | Source Table | Column |
|-------|--------------|--------|
| Name | `players_master` | `display_name` |
| Slug | `players_master` | `slug` |
| Birthdate/Age | `players_master` | `birthdate` |
| Hometown | `players_master` | `birth_city`, `birth_state_province`, `birth_country` |
| College | `players_master` | `school` |
| High School | `players_master` | `high_school` |
| Shoots | `players_master` | `shoots` |
| Position | `player_status` → `positions` | `position_code` or `raw_position` |
| Height | `player_status` | `height_in` |
| Weight | `player_status` | `weight_lb` |
| Wingspan | `combine_anthro` | `wingspan_in` (most recent by season) |

## Key Components

### Models (`app/models/players.py`)

**PlayerProfileRead** - Response model with computed properties for formatted display:
- `age_formatted`: Returns age as "19y 7m 12d" format
- `height_formatted`: Converts inches to feet'inches" (e.g., "6'9"")
- `weight_formatted`: Adds "lbs" suffix (e.g., "205 lbs")
- `wingspan_formatted`: Rounds to nearest half-inch, formats as feet'inches"
- `hometown`: Composes "City, State" for US, "City, Country" for international, or just country name if no city
- `position`: Falls back to `raw_position` if `position_code` is null

### Service (`app/services/player_service.py`)

**get_player_profile_by_slug()** - Fetches player data by joining:
1. `PlayerMaster` (base player info)
2. `PlayerStatus` (physical measurements, position)
3. `Position` (position code lookup)
4. `CombineAnthro` (wingspan from most recent season)

### Route (`app/routes/ui.py`)

The `player_detail` route:
- Fetches profile via service function
- Filters literal "null" strings from legacy data (college, high_school, shoots)
- Returns 404 if player not found
- Passes data to template with all metrics set to None (hides scoreboard)

## Data Cleaning

A `clean_null()` helper filters out literal "null" strings that exist in legacy data. This prevents the bio from displaying "c • null • 7'1" • 258 lbs" for players like Rudy Gobert who never attended college.

## Template Behavior

The player detail template (`app/templates/player-detail.html`):
- Conditionally renders each bio field only if data exists
- Hides the Draft Analytics Scoreboard when `metrics.consensusRank` is None
- Displays primary meta as "Position • College • Height • Weight" with bullet separators

## Testing

Integration tests in `tests/integration/test_player_profile.py` cover:
- Profile data retrieval and display
- Age format validation ("Xy Xm Xd")
- 404 for missing slugs
- Graceful handling of missing optional fields
- Scoreboard hiding when no metrics
- Wingspan from combine data
- Hometown formatting (US vs international)
- Literal "null" string filtering

## Future Work

- **Draft Analytics Scoreboard**: Currently hidden. Will need data sources for:
  - Consensus mock position
  - Draft buzz score
  - True draft position
  - Expected wins added
  - Stock trend

- **Player photos**: Currently uses placeholder images. Needs `photo_url` field.

- **Percentile data**: Currently hardcoded. Needs backend computation and storage.

## Related Commits

- `feat: Connect player profile page to database`
- `feat: Enhance hometown formatting for international players`
- `fix: Filter literal 'null' strings from player bio fields`
- `refactor: Remove legacy Players table and CRUD routes`
