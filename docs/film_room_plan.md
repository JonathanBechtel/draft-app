# Film Room (YouTube Videos) — Implementation Plan

## Context

DraftGuru needs a YouTube video feature ("Film Room") to add visual depth to the homepage and give prospect pages curated film study material. The feature spans three surfaces: a homepage section, a player detail section, and a dedicated `/film-room` page. Design mockup lives at `mockups/draftguru_film_room.html`.

The implementation follows the existing `PodcastShow`/`PodcastEpisode` pattern closely — same 3-phase ingestion, same polymorphic mention system, same thin-route/thick-service architecture.

---

## Phase 1: Data Model — Schemas, Migration, Config

### New files
- **`app/schemas/youtube_channels.py`** — `YouTubeChannel` table (mirrors `PodcastShow` in `app/schemas/podcast_shows.py`)
  - Fields: `id`, `name`, `display_name`, `channel_id` (unique YouTube ID), `channel_url`, `thumbnail_url`, `description`, `is_draft_focused`, `is_active`, `fetch_interval_minutes`, `last_fetched_at`, `created_at`, `updated_at`

- **`app/schemas/youtube_videos.py`** — `YouTubeVideo` table + `YouTubeVideoTag` enum (mirrors `PodcastEpisode` in `app/schemas/podcast_episodes.py`)
  - Enum values: `THINK_PIECE`, `CONVERSATION`, `SCOUTING_REPORT`, `HIGHLIGHTS`, `MONTAGE`
  - Fields: `id`, `channel_id` (FK), `external_id` (YouTube video ID), `title`, `description`, `youtube_url`, `thumbnail_url`, `duration_seconds`, `view_count`, `summary` (AI), `tag`, `published_at`, `created_at`, `player_id` (FK), `is_manually_added`
  - Unique constraint on `(channel_id, external_id)`

### Modified files
- **`app/schemas/player_content_mentions.py`** — Add `VIDEO = "video"` to `ContentType` enum
- **`app/config.py`** — Add `youtube_api_key: Optional[str] = None`
- **`app/services/admin_permission_service.py`** — Add `"youtube_channels"` and `"youtube_videos"` to `KNOWN_DATASETS`
- **`tests/integration/conftest.py`** — Add schema imports in `async_engine` fixture + `make_youtube_channel()` / `make_youtube_video()` factories
- **New Alembic migration** via `alembic revision --autogenerate`

---

## Phase 2: API Models

### New file
- **`app/models/videos.py`** — Response/request shapes (mirrors `app/models/podcasts.py`)
  - `YouTubeVideoRead`: `id`, `channel_name`, `thumbnail_url`, `title`, `summary`, `tag`, `youtube_url`, `youtube_embed_id`, `duration`, `time` (relative), `view_count_display`, `watch_on_text`, `is_player_specific`, `mentioned_players`
  - `VideoFeedResponse`: `items`, `total`, `limit`, `offset`
  - `YouTubeChannelRead`, `VideoIngestionResult`
  - Import `MentionedPlayer` from `app.models.podcasts` (refactor to shared location later if needed)

---

## Phase 3: Feed & Query Services

### New file
- **`app/services/video_service.py`** — Core data retrieval (mirrors `app/services/podcast_service.py`)
  - `get_video_feed(db, limit, offset, tag, channel_id, player_id, search)` — paginated feed with filters
  - `get_latest_videos_by_tag(db, tag, limit)` — for homepage tabbed playlist
  - `get_player_video_feed(db, player_id, limit, offset)` — UNION of mention-based + direct association (no backfill — player film study only shows relevant videos)
  - `get_player_video_counts_by_tag(db, player_id)` → `dict[str, int]` — tab counts + empty state detection
  - `get_video_page_data(db, ...)` — aggregated data for `/film-room` page
  - `get_video_player_filters(db, limit)` — most frequently tagged players for filter chips
  - `_load_mentions_for_videos(db, video_ids)` — batch mention loading
  - `_row_to_video_read()` — row-to-model mapper
  - `format_view_count()`, `parse_youtube_video_id()`, `parse_iso8601_duration()` — pure helpers

---

## Phase 4: Ingestion & Summarization Services

### New files
- **`app/services/video_summarization_service.py`** — Gemini AI classification (mirrors `app/services/podcast_summarization_service.py`)
  - `check_draft_relevance(title, description)` → bool
  - `analyze_video(title, description)` → `VideoAnalysis(summary, tag, mentioned_players)`
  - Prompt adapted for 5 video-specific tags

- **`app/services/video_ingestion_service.py`** — Hybrid curation pipeline (mirrors `app/services/podcast_ingestion_service.py`)
  - `run_ingestion_cycle(db)` — 3-phase: YouTube Data API fetch → batch insert → mention persistence
  - `add_video_by_url(db, youtube_url, tag, player_ids)` — manual addition from admin
  - `fetch_channel_videos(channel_id, api_key)` — YouTube Data API v3 calls (channels.list → playlistItems.list → videos.list)
  - YouTube API quota is ~30 units per cycle for 10 channels — well within 10K daily limit

---

## Phase 5: Routes — API, UI, Admin

### New files
- **`app/routes/videos.py`** — Public API
  - `GET /api/videos` — paginated feed (limit, offset, tag, channel_id, player_id, search)
  - `GET /api/videos/sources` — list channels (admin)
  - `POST /api/videos/sources` — create channel (admin)
  - `POST /api/videos/ingest` — trigger harvest (admin)
  - `POST /api/videos/add` — add single video by URL (admin)

- **`app/routes/admin/youtube_channels.py`** — Channel CRUD (mirrors `app/routes/admin/podcast_shows.py`)
  - List, create, edit, delete, trigger ingestion

- **`app/routes/admin/youtube_videos.py`** — Video management (mirrors `app/routes/admin/podcast_episodes.py`)
  - List with filters, edit, delete, manual add form

### Modified files
- **`app/main.py`** — Add `from app.routes import videos` + `app.include_router(videos.router)`
- **`app/routes/admin/__init__.py`** — Include youtube_channels and youtube_videos routers
- **`app/routes/ui.py`** — Three changes:
  1. New `/film-room` route → renders `film-room.html`
  2. Homepage `/` — fetch `film_room_videos` via `get_latest_videos_by_tag()`, pass to template
  3. Player detail `/players/{slug}` — fetch `player_video_counts` and `player_video_feed`, pass to template

---

## Phase 6: Frontend — Templates, CSS, JS

### New files
- **`app/static/css/film-room.css`** — Extract from mockup. Screening room aesthetic: dark bg, amber accents, film grain, sprocket borders, playlist layout, empty states
- **`app/static/js/film-room.js`** — All interactivity:
  - Tab switching (fetch `/api/videos?tag=X`, rebuild playlist)
  - Thumbnail click → swap iframe `src` + update metadata (single embed per page)
  - Grid card click → scroll to player, swap embed
  - Search (debounced fetch), type filters, player filters
  - Load More pagination
  - `buildYouTubeEmbed(videoId)` → `https://www.youtube.com/embed/${videoId}?rel=0&modestbranding=1`
- **`app/templates/film-room.html`** — Dedicated page (extends `base.html`)
- **`app/templates/partials/film-room-section.html`** — Homepage section partial
- **`app/templates/partials/film-study-section.html`** — Player page partial with empty state handling
- **Admin templates**: `admin/youtube-channels/{index,form}.html`, `admin/youtube-videos/{index,form,add}.html`

### Modified files
- **`app/templates/partials/navbar.html`** — Add "Film Room" link after "Podcasts"
- **`app/templates/home.html`** — Include `film-room-section.html` partial between News feed and Podcasts
- **`app/templates/player-detail.html`** — Include `film-study-section.html` partial between Combine Stats and Comparisons

### Empty state behavior (player pages)
- **No videos at all**: Collapsed `film-no-videos` dark bar — icon + "Film Study" + "No videos yet for {name}"
- **Empty tab**: Full section renders, empty tab gets dimmed `(0)` count, clicking shows centered message instead of playlist
- **Controlled by**: `player_video_counts` dict and `has_player_videos` bool from server

---

## Phase 7: Tests

### New files
- **`tests/integration/test_videos.py`** — Integration tests
  - `TestListVideos`: empty feed, populated feed, player_id filter, tag filter, search
  - `TestVideoSources`: auth required, create channel, duplicate rejection
  - `TestFilmRoomPage`: page renders, includes video data
  - `TestHomepageFilmRoom`: homepage includes film room section
  - `TestPlayerFilmStudy`: videos shown, no-videos placeholder

- **`tests/unit/test_video_utils.py`** — Pure function tests
  - `parse_youtube_video_id()` — various URL formats
  - `parse_iso8601_duration()` — PT1H2M3S → seconds
  - `format_view_count()` — 245000 → "245K views"

---

## Verification

After each phase:
1. `make precommit` — ruff + formatting + mypy on staged files
2. `mypy app --ignore-missing-imports` — full type check
3. `pytest tests/unit -q` — unit tests
4. `pytest tests/integration -q` — integration tests (requires DB)

After Phase 6 (frontend):
5. `make dev` + `make visual` — screenshot verification of all three surfaces

End-to-end:
6. Manually add a YouTube channel via admin, trigger ingestion, verify videos appear on homepage, Film Room page, and relevant player pages
7. Manually add a video by URL, verify player tagging works
8. Verify empty states on a player with no videos

---

## Key Reference Files

| Pattern | File to mirror |
|---------|---------------|
| Channel schema | `app/schemas/podcast_shows.py` |
| Video schema + enum | `app/schemas/podcast_episodes.py` |
| Feed service | `app/services/podcast_service.py` |
| Ingestion service | `app/services/podcast_ingestion_service.py` |
| AI summarization | `app/services/podcast_summarization_service.py` |
| Admin CRUD routes | `app/routes/admin/podcast_shows.py` |
| API response models | `app/models/podcasts.py` |
| Mention system | `app/schemas/player_content_mentions.py` |
| Design mockup | `mockups/draftguru_film_room.html` |
