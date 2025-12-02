# Player Similarity Frontend Integration

## Overview

This document describes the implementation of real-time player similarity data in the Player Comparisons section of the player detail page. Previously, this section used hardcoded mock data; it now fetches similarity scores from the `player_similarity` table via a new API endpoint.

## Architecture

### Data Flow

1. User visits `/players/{slug}` (player detail page)
2. `PlayerComparisonsModule` JavaScript initializes and calls the similarity API
3. API queries `player_similarity` table using global_scope snapshots
4. Results are displayed in cards with similarity scores
5. Clicking "Compare" opens a modal with head-to-head metrics from existing `/api/players/head-to-head` endpoint

### Database Query Pattern

The similarity API uses the existing indexes on `player_similarity` (~10M rows):
- `(anchor_player_id, snapshot_id)` - primary query path
- `(dimension, snapshot_id)` - filtering by dimension

Query returns max 10 rows (capped by `rank_within_anchor`), so JOINs are efficient.

## API Endpoint

### `GET /api/players/{slug}/similar`

Returns similar players for a given player and similarity dimension.

**Parameters:**
| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `dimension` | enum | Yes | - | `anthro`, `combine`, `shooting`, or `composite` |
| `same_position` | bool | No | `false` | Filter to players with matching position |
| `same_draft_year` | bool | No | `false` | Filter to players with same draft year as anchor |
| `nba_only` | bool | No | `false` | Filter to active NBA players only |
| `limit` | int | No | `10` | Max results (1-20) |

**Response:**
```json
{
  "anchor_slug": "cooper-flagg",
  "dimension": "anthro",
  "snapshot_id": 1520,
  "players": [
    {
      "slug": "cory-jefferson",
      "display_name": "Cory Jefferson",
      "position": "pf",
      "school": "Baylor",
      "draft_year": 2014,
      "similarity_score": 91.55,
      "rank": 1,
      "shared_position": false
    }
  ]
}
```

## Frontend Mapping

### Category Tabs to API Dimensions
| UI Tab | API Dimension |
|--------|---------------|
| Anthropometrics | `anthro` |
| Combine Performance | `combine` |
| Shooting | `shooting` |

### Pool Dropdown to Filters
| Pool Option | API Parameters |
|-------------|----------------|
| Current Draft Class | `same_draft_year=true` |
| Historical Prospects | (no filters) |
| Active NBA Players | `nba_only=true` |

### Position Checkbox
| Checkbox State | API Parameter |
|----------------|---------------|
| Checked | `same_position=true` |
| Unchecked | `same_position=false` |

## Files Changed

### Backend (New)
- `app/models/similarity.py` - Pydantic response models (`SimilarPlayer`, `PlayerSimilarityResponse`)
- `app/services/similarity_service.py` - Query logic with snapshot selection and filtering
- `tests/integration/test_similarity.py` - Integration tests

### Backend (Modified)
- `app/routes/players.py` - Added `/api/players/{slug}/similar` endpoint
- `app/routes/ui.py` - Removed mock `comparison_data` list

### Frontend (Modified)
- `app/templates/player-detail.html` - Renamed "Advanced Stats" tab to "Shooting"
- `app/static/js/player-detail.js` - Rewrote `PlayerComparisonsModule` to fetch from API
- `app/static/css/player-detail.css` - Added styles for prominent badges, clickable images, winner highlighting

## UI Enhancements

### Similarity Badge
- Larger and more prominent with gradient backgrounds
- Shows "X% Match" text
- Box shadow for depth
- Hover animation (scales up)

### Clickable Card Images
- Entire image area links to player's detail page
- Hover effect scales the image

### Comparison Modal
- Winner highlighting: values where a player "wins" are highlighted in fuchsia
- Winner banner: shows overall lead (e.g., "Cooper Flagg leads 5-3")
- Respects `lower_is_better` flag for metrics like sprint times

## Snapshot Selection

The API prefers `global_scope` cohort snapshots:
1. First attempts to find `MetricSnapshot` with `cohort=global_scope` and `is_current=True`
2. Falls back to any current snapshot for the source if no global snapshot exists

This ensures consistent similarity scores across all seasons/cohorts.

## Caching

The frontend caches API responses by filter state to avoid redundant requests when switching between tabs or toggling filters back to previous states.

Cache key format: `{dimension}|{same_position}|{same_draft_year}|{nba_only}`
