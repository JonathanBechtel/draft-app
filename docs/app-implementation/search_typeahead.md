# Search Typeahead Implementation

**Date:** 2025-11-30
**Branch:** `feature/implement-search`
**Commit:** (see git log)

## Overview

Implemented typeahead search functionality that filters player names as the user types, displaying results in a dropdown. Clicking or selecting a result navigates to the player detail page using stable, database-stored slugs.

## Files Created

| File | Description |
|------|-------------|
| `app/utils/slug.py` | Slug generation utilities with collision handling |
| `alembic/versions/f1a2b3c4d5e6_add_slug_to_players_master.py` | Migration to add slug column and backfill existing players |

## Files Modified

| File | Changes |
|------|---------|
| `app/schemas/players_master.py` | Added `slug` field with unique constraint and index |
| `app/models/players.py` | Added `PlayerSearchResult` response model |
| `app/routes/players.py` | Added `GET /players/search` endpoint |
| `app/templates/partials/navbar.html` | Added dropdown container, autocomplete attribute |
| `app/static/main.css` | Added search results dropdown styles (~60 lines) |
| `app/static/main.js` | Replaced with typeahead implementation (~250 lines) |

## Architecture Decisions

### Slug Storage Strategy
- **Decision:** Store slugs in database rather than auto-generate at runtime
- **Rationale:**
  - URL stability: slugs remain constant even if player names change (important for SEO)
  - Collision handling: explicit suffixes (`john-smith`, `john-smith-2`) stored permanently
  - Performance: direct DB lookup by indexed slug column

### JavaScript Approach
- **Kept in main.js:** Search is app-wide (navbar on every page), so global location is appropriate
- **Genuine functionality:** Keyboard navigation, debouncing, and API integration require JS
- **CSS for visuals:** Show/hide states, hover effects, selection highlighting handled via CSS classes

### Backend vs Frontend Logic
- **Backend:** Search query (ILIKE), ordering, limit (10 results), slug generation
- **Frontend:** Debouncing (300ms), result rendering, navigation

## API Design

### `GET /players/search?q={query}`

**Parameters:**
- `q` (required): Search query, minimum 1 character

**Response** (200 OK):
```json
[
  {
    "id": 1,
    "display_name": "Cooper Flagg",
    "slug": "cooper-flagg",
    "school": "Duke"
  }
]
```

**Implementation:**
- Case-insensitive partial match on `display_name` using ILIKE
- Returns up to 10 results ordered alphabetically
- Queries `PlayerMaster` table

## UX Behavior

1. User types in search input
2. After 300ms pause (debounce), fetch matching players from API
3. Display dropdown with up to 10 results showing name + school
4. **Keyboard navigation:**
   - Arrow Up/Down: Navigate through results
   - Enter: Select highlighted result (or first if none highlighted)
   - Escape: Close dropdown and blur input
5. **Mouse interaction:**
   - Hover: Highlights result
   - Click: Navigates to player page
6. Selection navigates to `/players/{slug}`
7. Click outside or blur closes dropdown

## CSS Components

```css
.search-results        /* Dropdown container, hidden by default */
.search-results.active /* Visible state */
.search-result-item    /* Individual result row */
.search-result-item.selected /* Keyboard-selected state */
.search-result-name    /* Player name text */
.search-result-school  /* School name text */
.search-results-empty  /* "No players found" message */
```

## Slug Utility Functions

### `generate_slug(name: str) -> str`
Converts display name to URL-safe slug:
- Normalizes unicode (é → e)
- Lowercases
- Replaces spaces/underscores with hyphens
- Removes non-alphanumeric characters
- Collapses multiple hyphens

### `generate_unique_slug(name, db, exclude_id) -> str`
Async function that generates unique slug with collision handling:
- Checks database for existing slugs
- Appends numeric suffix if collision (`-2`, `-3`, etc.)
- `exclude_id` parameter for updating existing players

### `generate_slug_sync(name, existing_slugs) -> str`
Synchronous version for use in migrations with in-memory collision tracking.

## Migration Notes

The migration (`f1a2b3c4d5e6`) performs:
1. Add nullable `slug` column
2. Backfill all existing players with generated slugs (handling collisions)
3. Create unique index on `slug` column

Run with: `alembic upgrade head`
Rollback with: `alembic downgrade -1`

## Scope Notes

**Implemented:** Search typeahead with navigation to player pages

**Deferred:** Connecting player detail page (`/players/{slug}`) to real database data. Currently player pages render with hardcoded mock data regardless of which slug is requested. This will be addressed in a future iteration.

## Testing

Manual verification:
- `make precommit` passes (ruff, ruff-format, mypy)
- Search API returns correct JSON with slugs
- Collision handling works (e.g., `aj-lawson-2` in results)
- Keyboard and mouse navigation functional
