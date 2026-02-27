# Trending Players Refactor Plan

## Problem

Trending player logic (`get_trending_players`, `TrendingPlayer`, `_get_daily_mention_counts`) lives in `news_service.py` even though it's used by three pages: homepage, news, and podcasts. The function is already parameterized for cross-content-type use, but its location is misleading and the calling pages are inconsistent in what data they pass to templates.

## Current State

### Service function (in `news_service.py`)

```python
async def get_trending_players(
    db: AsyncSession,
    days: int = 7,
    limit: int = 10,
    content_type: ContentType | None = None,
) -> list[TrendingPlayer]
```

- `content_type=None` aggregates across all types (news + podcasts)
- Uses linear decay weighting: `weight = max(1.0 - age_days / days, 0.0)`
- Calls `_get_daily_mention_counts()` for sparkline data
- Returns full `TrendingPlayer` dataclass with daily_counts, trending_score, etc.

### Call sites

| Page | days | limit | content_type | Data passed to template |
|------|------|-------|-------------|------------------------|
| Homepage (`/`) | 7 | 10 | `None` (all) | Full: daily_counts, trending_score, school |
| News (`/news`) | 30 | 10 | `NEWS` | Minimal: name, count only |
| Podcasts (`/podcasts`) | 7 | 7 | `PODCAST` | Full, but rendered minimally |

### Import sites that reference trending from `news_service`

1. `app/routes/ui.py` — imports `get_trending_players`
2. `app/services/podcast_service.py` — imports `TrendingPlayer`, `get_trending_players`
3. `tests/integration/test_trending.py` — imports `get_trending_players`

## Plan

### Step 1: Move trending logic to `player_mention_service.py`

Move these three items out of `news_service.py` into `app/services/player_mention_service.py`:

- `TrendingPlayer` dataclass
- `get_trending_players()` function
- `_get_daily_mention_counts()` helper

This file already handles player name resolution (`resolve_player_names`, `PlayerMatch`, etc.), so mention-related analytics are a natural fit. No signature changes needed.

### Step 2: Update imports (3 files)

- `app/routes/ui.py` — change import source from `news_service` to `player_mention_service`
- `app/services/podcast_service.py` — same
- `tests/integration/test_trending.py` — same

### Step 3: Standardize the `days` window

Use **14 days** everywhere as the default lookback, balancing recency with data sparsity. The linear decay weighting already handles freshness — a 14-day window won't surface stale players because their weights approach 0.

| Page | days (before) | days (after) |
|------|--------------|-------------|
| Homepage | 7 | 14 |
| News | 30 | 14 |
| Podcasts | 7 | 14 |

### Step 4: Pass full `TrendingPlayer` data from all routes

The news route currently strips `school`, `trending_score`, and `daily_counts` when building the template context dict. Pass the full set so the frontend *could* render sparklines or richer displays without a backend change:

```python
# Before (news route)
{"player_id": tp.player_id, "display_name": tp.display_name,
 "slug": tp.slug, "mention_count": tp.mention_count}

# After (all routes, shared shape)
{"player_id": tp.player_id, "display_name": tp.display_name,
 "slug": tp.slug, "school": tp.school or "",
 "mention_count": tp.mention_count, "trending_score": tp.trending_score,
 "daily_counts": tp.daily_counts}
```

The frontend modules can choose to use or ignore the extra fields — no JS changes required unless we want to add sparklines to the sidebars later.

### Step 5: Add re-export from `news_service.py` (optional, temporary)

To avoid breaking any imports we missed, add a deprecation-style re-export:

```python
# app/services/news_service.py
from app.services.player_mention_service import (
    TrendingPlayer,
    get_trending_players,
)
```

Remove this after confirming all call sites are updated.

## Files touched

| File | Change |
|------|--------|
| `app/services/player_mention_service.py` | Add `TrendingPlayer`, `get_trending_players`, `_get_daily_mention_counts` |
| `app/services/news_service.py` | Remove the above (~160 lines), optionally add re-exports |
| `app/routes/ui.py` | Update import, standardize days=14, pass full data for news trending |
| `app/services/podcast_service.py` | Update import, standardize days=14 |
| `tests/integration/test_trending.py` | Update import |

## Out of scope

- Adding sparklines to the news/podcast sidebars (future frontend work)
- Changing the decay algorithm (linear decay is fine for now)
- Adding new content types to `ContentType` enum
