# Player Tags Feature — Code Review

**Date**: 2026-02-08
**Branch**: `feature/player-tags`
**Scope**: Working tree (11 modified, 9 new files)

## Checks

| Check | Result |
|-------|--------|
| Unit tests (`pytest tests/unit -q`) | 59 passed |
| mypy (`mypy app --ignore-missing-imports`) | 0 errors |
| Lint (`ruff check .`) | All checks passed |

## Regressions

- **None found.**
- `player_id` parameter removed from `get_news_feed()` — confirmed no remaining callers use it. Replaced by dedicated `get_player_news_feed()`.
- `ingest_rss_source` return type changed from `tuple[int, int]` to `tuple[int, int, int]` — the only caller (`run_ingestion_cycle`) is updated to match.
- New fields on `IngestionResult` (`mentions_added`) and `NewsItemRead` (`is_player_specific`) both have defaults, so existing consumers are unaffected.

## Issues Found & Fixes Applied

### 1. `union_all` should be `union` in player feed query (Fixed)

**File**: `app/services/news_service.py:279`

`union_all(mention_subq, direct_subq)` does not deduplicate. If an article has both a mention row AND `NewsItem.player_id` set, its ID appears twice in the subquery. The outer `IN` clause deduplicates implicitly, so results were correct — but `union()` communicates intent better.

**Fix**: Changed `union_all` to `union`.

### 2. Missing story filter buttons for two tag types (Fixed)

**File**: `app/templates/home.html:28-38`

The story type filter buttons did not include "Skill Theme" or "Statistical Analysis" tags, though both are valid `NewsItemTag` values that Gemini can return. Articles tagged with these types were not filterable from the homepage UI.

**Fix**: Added "Skill Theme" and "Stats" (Statistical Analysis) filter buttons.

### 3. `MentionSource.source` column type in migration (Not fixed — acceptable)

**File**: `alembic/versions/h7i8j9k0l1m2_add_player_mentions.py:34`

The migration declares `sa.Column("source", sa.String())` but the SQLModel schema uses `MentionSource` (a `str` enum). This works because SQLModel stores `str` Enum values as plain strings. If DB-level enum enforcement is desired later, this would need updating. Fine for now.

### 4. Backfill script loads all news items into memory (Not fixed — acceptable at current scale)

**File**: `scripts/backfill_player_mentions.py:104-112`

`result.all()` fetches all `NewsItem` rows at once. For very large datasets this could be memory-intensive. Consider batching with `.yield_per()` or `LIMIT/OFFSET` if the news table grows large.

## Duplications

### Test fixture overlap (acceptable)

`_make_article()` is defined separately in both `tests/integration/test_trending.py` and `tests/integration/test_player_feed.py` with slightly different signatures. The `news_source` fixture is also duplicated. Consider extracting into a shared `tests/integration/factories.py` when the integration test count grows further. Low priority since signatures differ.

No significant production code duplication was found. The `_NEWS_FEED_COLUMNS` shared column list is well DRY'd across `get_news_feed`, `get_hero_article`, and `get_player_news_feed`.

## Architecture Notes (Positive)

- **3-phase ingestion**: Network/AI work -> DB insert -> mention persistence. Each phase has its own transaction boundary with good error isolation.
- **`resolve_player_names_as_map()`**: Keying by lowered input name is a smart design for handling alias mismatches (e.g., "D.J. Harper" -> Dylan Harper's player_id).
- **`_NEWS_FEED_COLUMNS`**: Shared column list with "keep in sync" comment is good practice.
- **Integration tests**: Thorough coverage of mention-based feeds, direct `player_id`, dedup, backfill, and the API endpoint.
- **`is_player_specific` flag**: Clean way to let the UI differentiate player-relevant vs backfilled articles.

## Files Changed

### Modified
- `app/models/news.py` — Added `is_player_specific` field to `NewsItemRead`, `mentions_added` to `IngestionResult`
- `app/routes/news.py` — Route delegates to `get_player_news_feed` when `player_id` is provided
- `app/routes/ui.py` — Homepage renders trending players section; player detail uses player-specific feed
- `app/schemas/players_master.py` — Added `is_stub` field
- `app/services/news_ingestion_service.py` — Phase 3 mention persistence; AI-detected player names resolved and stored
- `app/services/news_service.py` — `get_trending_players`, `get_player_news_feed` (UNION-based), shared `_NEWS_FEED_COLUMNS`
- `app/services/news_summarization_service.py` — Gemini prompt extended to extract `mentioned_players`; `ArticleAnalysis` model updated
- `app/static/css/home.css` — Trending player card styles
- `app/static/js/home.js` — `TrendingModule` for rendering trending players
- `app/templates/home.html` — Trending section markup, story filter buttons
- `tests/integration/conftest.py` — Added `news_item_player_mentions` schema import

### New
- `alembic/versions/h7i8j9k0l1m2_add_player_mentions.py` — Migration: junction table + `is_stub` column
- `app/schemas/news_item_player_mentions.py` — `NewsItemPlayerMention` junction table schema
- `app/services/player_mention_service.py` — Name resolution service (display_name, alias, stub creation)
- `scripts/backfill_player_mentions.py` — Backfill script for existing articles
- `tests/integration/test_player_feed.py` — Player feed service + API integration tests
- `tests/integration/test_player_mentions.py` — Player mention resolution integration tests
- `tests/integration/test_trending.py` — Trending players integration tests
- `tests/unit/test_news_summarization.py` — Parse response unit tests (mentioned_players handling)
- `tests/unit/test_player_mention_service.py` — `split_name` unit tests
