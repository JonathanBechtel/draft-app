# Film Room (YouTube Videos) — Implementation Plan (Revised)

## Context

DraftGuru needs a YouTube video feature ("Film Room") to add visual depth to the homepage and give prospect pages curated film study material. The feature spans three surfaces: a homepage section, a player detail section, and a dedicated `/film-room` page. Design mockup lives at `mockups/draftguru_film_room.html`.

This revised plan still follows the existing podcast/news architecture (thin routes, thick services, offline enrichment), but closes technical gaps around enum migrations, player association consistency, ingestion durability, and data lifecycle behavior.

---

## Revision Notes (What Changed)

- [CHANGED-01] **Player association source of truth is now explicit**: `player_content_mentions` is canonical for video-to-player links; `youtube_videos.player_id` is removed to avoid dual-write drift.
- [CHANGED-02] **Enum migration is now explicit**: adding `VIDEO` to `contenttype` requires a hand-authored Alembic step (`ALTER TYPE ... ADD VALUE`) in an Alembic `autocommit_block`, rather than relying only on autogenerate.
- [CHANGED-03] **Permission model expanded**: added separate `youtube_ingestion` dataset permission, mirroring existing `news_ingestion` / `podcast_ingestion` separation.
- [CHANGED-04] **Ingestion strategy made incremental and quota-safe**: explicit pagination, cutoff logic, retry behavior, and last-fetch semantics.
- [CHANGED-05] **Lifecycle cleanup rules added**: mention rows are updated/deleted transactionally when videos are edited/deleted.
- [CHANGED-06] **Model coupling reduced**: `MentionedPlayer` moved to a shared model module instead of importing from podcasts.
- [CHANGED-07] **Index/search plan added**: concrete indexes + search behavior to keep feed queries cheap.
- [CHANGED-08] **Test plan expanded**: adds ingestion failure-mode coverage, permission tests, and mention integrity tests.
- [CHANGED-09] **Behavior-first phase test matrix added**: each phase now has explicit behavior outcomes (not just narrow unit assertions).
- [CHANGED-10] **Migration validation updated for your workflow**: migration testing is scoped to a branched copy of the existing DB, not a fresh/empty DB.
- [CHANGED-11] **Config/env scope clarified**: add `settings.youtube_api_key` and `.env` support for `YOUTUBE_API_KEY`.
- [CHANGED-12] **Admin permission wiring scope clarified**: dataset permissions must be wired through admin auth checks, sidebar visibility, and user permission editor UI.
- [CHANGED-13] **Manual mention semantics clarified**: manual player tags are additive to AI tags (AI rows are preserved).
- [CHANGED-14] **Ingestion watermark semantics clarified**: `last_fetched_at` updates only after both video persistence and mention persistence succeed for that channel.

---

## Phase 1: Data Model — Schemas, Migration, Config

### New files

- **`app/schemas/youtube_channels.py`** — `YouTubeChannel` table
  - Fields: `id`, `name`, `display_name`, `channel_id` (unique), `channel_url`, `thumbnail_url`, `description`, `uploads_playlist_id`, `is_draft_focused`, `is_active`, `fetch_interval_minutes`, `last_fetched_at`, `created_at`, `updated_at`
  - [CHANGED-04] `uploads_playlist_id` is persisted to avoid repeated `channels.list` lookups every cycle.

- **`app/schemas/youtube_videos.py`** — `YouTubeVideo` table + `YouTubeVideoTag` enum
  - Enum values: `THINK_PIECE`, `CONVERSATION`, `SCOUTING_REPORT`, `HIGHLIGHTS`, `MONTAGE`
  - Fields: `id`, `channel_id` (FK), `external_id` (YouTube video ID), `title`, `description`, `youtube_url`, `thumbnail_url`, `duration_seconds`, `view_count`, `summary`, `tag`, `published_at`, `created_at`, `is_manually_added`
  - [CHANGED-01] **No `player_id` column**; player association is canonical in `player_content_mentions`.
  - [CHANGED-07] Add indexes:
    - `ix_youtube_videos_published_at` on `published_at`
    - `ix_youtube_videos_channel_published` on `(channel_id, published_at)`
    - `ix_youtube_videos_tag_published` on `(tag, published_at)`
    - `ix_youtube_videos_external_id` unique on `external_id`
  - [CHANGED-07] Add check constraints for non-negative `duration_seconds` and `view_count`.

### Modified files

- **`app/schemas/player_content_mentions.py`**
  - Add `VIDEO = "video"` to `ContentType` enum.
  - [CHANGED-02] Ensure migration handles underlying Postgres enum label `VIDEO` correctly.

- **`app/config.py`**
  - Add `youtube_api_key: Optional[str] = None`
  - [CHANGED-11] Configure `.env` with `YOUTUBE_API_KEY=...` (read by `settings.youtube_api_key`).

- **`app/services/admin_permission_service.py`**
  - Add `"youtube_channels"`, `"youtube_videos"`, and [CHANGED-03] `"youtube_ingestion"` to `KNOWN_DATASETS`.

- **`tests/integration/conftest.py`**
  - Add schema imports in `async_engine` fixture
  - Add `make_youtube_channel()` / `make_youtube_video()` factories

### Migration requirements

- **New Alembic migration** (autogenerate + manual edits)
  - Create `youtube_channels` and `youtube_videos` tables + indexes/constraints.
  - [CHANGED-02] Add explicit enum migration for `contenttype`:
    - Upgrade: in `op.get_context().autocommit_block()`, run `ALTER TYPE contenttype ADD VALUE IF NOT EXISTS 'VIDEO'`
    - Downgrade: document non-trivial enum value removal; do not silently generate unsafe downgrade.
  - [CHANGED-10] Verify against a branched copy of the existing DB: `upgrade head` + functional smoke + downgrade-path check.

---

## Phase 2: API Models

### New files

- **`app/models/content_mentions.py`**
  - [CHANGED-06] Move shared `MentionedPlayer` here for reuse by podcasts/videos/news.

- **`app/models/videos.py`**
  - `YouTubeVideoRead`: `id`, `channel_name`, `thumbnail_url`, `title`, `summary`, `tag`, `youtube_url`, `youtube_embed_id`, `duration`, `time`, `view_count_display`, `watch_on_text`, `is_player_specific`, `mentioned_players`
  - `VideoFeedResponse`: `items`, `total`, `limit`, `offset`
  - `YouTubeChannelRead`, `YouTubeChannelCreate`, `YouTubeChannelUpdate`
  - `VideoIngestionResult`
  - `ManualVideoAddRequest` and `ManualVideoUpdateRequest`

---

## Phase 3: Feed & Query Services

### New file

- **`app/services/video_service.py`**
  - `get_video_feed(db, limit, offset, tag, channel_id, player_id, search)`
  - `get_latest_videos_by_tag(db, tag, limit)`
  - `get_player_video_feed(db, player_id, limit, offset)`
    - [CHANGED-01] Uses mention rows (`ContentType.VIDEO`) as the only player-link source.
  - `get_player_video_counts_by_tag(db, player_id)`
  - `get_video_page_data(db, ...)`
  - `get_video_player_filters(db, limit)`
  - `_load_mentions_for_videos(db, video_ids)`
  - `_row_to_video_read()`
  - `format_view_count()`, `parse_youtube_video_id()`, `parse_iso8601_duration()`

### Query/Performance requirements

- [CHANGED-07] Apply deterministic ordering (`published_at DESC, id DESC`) across all list endpoints.
- [CHANGED-07] Search implementation:
  - v1: case-insensitive `ILIKE` on `title` and `summary`
  - keep query composable; optional future `pg_trgm` upgrade can be added without API contract changes.
- [CHANGED-07] Keep count and item query filters exactly in sync to avoid pagination mismatches.

---

## Phase 4: Ingestion & Summarization Services

### New files

- **`app/services/video_summarization_service.py`**
  - `check_draft_relevance(title, description)` -> bool
  - `analyze_video(title, description)` -> `VideoAnalysis(summary, tag, mentioned_players)`
  - Prompt adapted for 5 video-specific tags.

- **`app/services/video_ingestion_service.py`**
  - `run_ingestion_cycle(db)`
  - `add_video_by_url(db, youtube_url, tag, player_ids)`
  - `fetch_channel_videos(channel, api_key, cutoff)`

### Ingestion contract

- [CHANGED-04] 3-phase ingestion is explicit:
  1. **Fetch phase (no DB transaction):** YouTube API pagination + enrichment.
  2. **Persist phase (short transaction):** bulk upsert videos.
  3. **Mention/finalize phase (short transaction):** resolve players, insert mention rows with conflict handling, then update `last_fetched_at`.

- [CHANGED-04] Incremental/pagination behavior:
  - Prefer cached `uploads_playlist_id`; only call `channels.list` when missing.
  - Walk `playlistItems.list` with `pageToken` until cutoff reached or `max_pages_per_channel` threshold hit.
  - Batch `videos.list` calls (up to 50 ids per call).
  - Filter candidates by `published_at > (last_fetched_at - safety_buffer)`.

- [CHANGED-04] Quota and resilience:
  - Handle 429/5xx with bounded retries/backoff.
  - Process channels independently; one channel failure must not fail the whole cycle.
  - [CHANGED-14] Update `last_fetched_at` only after successful mention/finalize phase for that channel (not immediately after video upsert).

- [CHANGED-05] Mention lifecycle rules:
  - For newly ingested videos: insert AI mentions (`source=AI`) idempotently.
  - [CHANGED-13] For manual video edits: treat `source=MANUAL` rows as additive to AI rows. In one transaction, upsert submitted MANUAL rows, delete stale MANUAL rows for that video, and preserve all `source=AI` rows.
  - For video delete: delete mentions for `(content_type=VIDEO, content_id=video_id)` in same transaction.

---

## Phase 5: Routes — API, UI, Admin

### New files

- **`app/routes/videos.py`** — Public/API endpoints
  - `GET /api/videos`
  - `GET /api/videos/sources` (admin)
  - `POST /api/videos/sources` (admin)
  - `POST /api/videos/ingest` (admin)
  - `POST /api/videos/add` (admin)

- **`app/routes/admin/youtube_channels.py`**
  - List/create/edit/delete channel; trigger ingestion.

- **`app/routes/admin/youtube_videos.py`**
  - List/edit/delete videos; manual add/update player tags.

### Modified files

- **`app/main.py`**
  - Add `videos` router import + include.

- **`app/routes/admin/__init__.py`**
  - Include `youtube_channels` and `youtube_videos` routers.

- **`app/routes/admin/helpers.py`**
  - [CHANGED-12] Ensure dataset access checks are used consistently for `youtube_channels`, `youtube_videos`, and `youtube_ingestion`.

- **`app/templates/admin/base.html`**
  - [CHANGED-12] Add sidebar visibility wiring for new video datasets (channels, videos, ingestion views).

- **`app/templates/admin/users/permissions.html`**
  - [CHANGED-12] Ensure new datasets appear in permission editor UI with view/edit toggles.

- **`app/routes/ui.py`**
  1. New `/film-room` route -> `film-room.html`
  2. Homepage `/` -> fetch `film_room_videos` via `get_latest_videos_by_tag()`
  3. Player detail `/players/{slug}` -> fetch `player_video_counts` + `player_video_feed`

### Authz requirements

- [CHANGED-03] Permission boundaries:
  - Channel CRUD -> `youtube_channels`
  - Video CRUD/manual add -> `youtube_videos`
  - Ingestion trigger -> `youtube_ingestion`
  - [CHANGED-12] Worker visibility in admin sidebar and permission editor must reflect these datasets exactly.

---

## Phase 6: Frontend — Templates, CSS, JS

### New files

- **`app/static/css/film-room.css`**
- **`app/static/js/film-room.js`**
- **`app/templates/film-room.html`**
- **`app/templates/partials/film-room-section.html`**
- **`app/templates/partials/film-study-section.html`**
- **Admin templates**
  - `admin/youtube-channels/{index,form}.html`
  - `admin/youtube-videos/{index,form,add}.html`

### Modified files

- **`app/templates/partials/navbar.html`** — add "Film Room" link.
- **`app/templates/home.html`** — include homepage film room section.
- **`app/templates/player-detail.html`** — include player film study section.

### Frontend implementation constraints

- [CHANGED-07] Render user/content strings safely; avoid unsafe HTML interpolation for titles/summaries.
- Keep one active iframe per view; update `src` only when selected video changes.
- `buildYouTubeEmbed(videoId)` -> `https://www.youtube.com/embed/${videoId}?rel=0&modestbranding=1`

### Empty state behavior (player pages)

- No videos at all: collapsed `film-no-videos` bar.
- Empty tab: section stays rendered; tab shows `(0)`; center empty message on click.
- Controlled by `player_video_counts` and `has_player_videos`.

---

## Phase 7: Tests

### New files

- **`tests/integration/test_videos.py`**
  - `TestListVideos`: empty feed, populated feed, tag/channel/player/search filters
  - `TestVideoSources`: auth required, create channel, duplicate rejection
  - `TestVideoIngestionAuth`: ingestion endpoint requires `youtube_ingestion`
  - `TestFilmRoomPage`: page renders and includes initial data
  - `TestHomepageFilmRoom`: homepage section presence
  - `TestPlayerFilmStudy`: player-specific videos + no-video placeholder
  - [CHANGED-05] `TestVideoDeleteCleansMentions`
  - [CHANGED-13] `TestManualTagEditPreservesAIMentionsAndReconcilesManualMentions`

- **`tests/unit/test_video_utils.py`**
  - `parse_youtube_video_id()` URL variants
  - `parse_iso8601_duration()`
  - `format_view_count()`

- **`tests/unit/test_video_ingestion_service.py`**
  - [CHANGED-04] pagination/cutoff behavior
  - dedupe behavior on conflict
  - retries on 429/5xx
  - channel-isolated failure handling

### Test coverage expectations

- [CHANGED-08] Include at least one integration test asserting `ContentType.VIDEO` mentions contribute to player feed/trending queries correctly.

---

## Phase-By-Phase Test Matrix (Behavior-First)

### Phase 1 — Data Model / Migration

| Layer | What to test | Pass criteria |
|---|---|---|
| Migration (existing DB branch) | Apply migration on a branched copy of current DB data | `alembic upgrade head` succeeds with no data loss/errors |
| DB behavior | Insert/select `ContentType.VIDEO` mentions and valid channel/video rows | Rows persist and query correctly |
| Constraint behavior | Duplicate `external_id`; negative `view_count`/`duration_seconds` | Invalid writes are rejected by DB |
| Regression smoke | Run existing news/podcast/trending read paths after migration | Existing features still function |

### Phase 2 — API Models

| Layer | What to test | Pass criteria |
|---|---|---|
| Unit | Request/response model validation | Valid payloads pass; invalid payloads fail with clear errors |
| Integration | Response shapes from live endpoints | JSON contract matches declared fields/types |
| Regression | Shared `MentionedPlayer` usage across media | No schema drift between podcasts/videos |

### Phase 3 — Feed & Query Services

| Layer | What to test | Pass criteria |
|---|---|---|
| Unit | Video helper functions | Edge cases handled deterministically |
| Integration | Filter combinations (`tag`, `channel`, `player`, `search`) | Correct result set and correct `total` |
| Integration | Ordering/pagination stability | Stable order; no duplicates/missing rows across pages |
| Behavior | Player feed relevance via mentions | No unrelated videos in player feed |

### Phase 4 — Ingestion / Summarization

| Layer | What to test | Pass criteria |
|---|---|---|
| Unit (mock network/AI) | Pagination, cutoff, retry logic | Stops at cutoff; retries bounded; failures classified |
| Integration (DB + mocked APIs) | First ingest then re-ingest | First run inserts; second run is idempotent |
| Integration | Per-channel failure isolation | One failing channel does not block others |
| Behavior | Mention source semantics | AI ingest writes `source=AI`; manual curation writes additive `source=MANUAL` rows while preserving AI rows |

### Phase 5 — Routes / Auth / Admin

| Layer | What to test | Pass criteria |
|---|---|---|
| Integration API | Public endpoint success/failure paths | Correct status codes and payload shapes |
| Integration Authz | Dataset matrix (`youtube_channels`, `youtube_videos`, `youtube_ingestion`) | Access allow/deny matches policy |
| Integration Authz/UI | Sidebar + permission editor wiring for video datasets | Worker users only see/operate on datasets they are permitted for |
| Integration Admin | CRUD + manual add/edit/delete flows | Correct DB mutations and redirect states |
| Behavior | Mention cleanup on edit/delete | No orphan `player_content_mentions` rows |

### Phase 6 — Frontend Behavior

| Layer | What to test | Pass criteria |
|---|---|---|
| UI integration | `/film-room` tab/filter/search/load-more/embed | UI updates correctly for user interactions |
| UI integration | Homepage Film Room section | Loads content and handles empty state gracefully |
| UI integration | Player film section | Correct tab counts and no-videos/empty-tab states |
| Visual | `make visual` screenshots | No unintended regressions on touched surfaces |

### Phase 7 — End-to-End / Release Gate

| Layer | What to test | Pass criteria |
|---|---|---|
| E2E manual | Channel create -> ingest -> verify all three surfaces | Videos appear correctly on homepage, `/film-room`, and player page |
| E2E manual | Manual add/edit/delete + player tags | Feed, filtering, and mention lifecycle are correct |
| Behavior | Homepage trending aggregation | Trending includes video mentions with other media types |
| Ops | Ingestion observability | Logs/metrics are sufficient to debug per-channel outcomes |

---

## Verification

After each phase:
1. `make precommit` — ruff + formatting + mypy (hook scope)
2. `mypy app --ignore-missing-imports` — full app type check
3. `pytest tests/unit -q`
4. `pytest tests/integration -q` (DB required)

After Phase 6:
5. `make dev` + `make visual` — screenshot verification for homepage, player page, `/film-room`

Migration-specific:
6. [CHANGED-10] Run migration upgrade on a branched copy of the existing DB and verify enum update + new tables/indexes
7. [CHANGED-10] Run downgrade-path verification on that branch and document any intentional enum-downgrade caveat

End-to-end:
8. Add a channel via admin, ingest, verify videos on homepage, `/film-room`, and relevant player pages
9. Add video by URL with manual tags, verify mentions and player filtering
10. Delete/edit a video, verify mention lifecycle behavior is correct

---

## Key Reference Files

| Pattern | File to mirror |
|---------|---------------|
| Channel schema | `app/schemas/podcast_shows.py` |
| Content schema | `app/schemas/podcast_episodes.py` |
| Feed service | `app/services/podcast_service.py` |
| Ingestion service | `app/services/podcast_ingestion_service.py` |
| AI summarization | `app/services/podcast_summarization_service.py` |
| Admin CRUD routes | `app/routes/admin/podcast_shows.py` |
| Shared mentions model | `app/models/content_mentions.py` |
| Mention system | `app/schemas/player_content_mentions.py` |
| Design mockup | `mockups/draftguru_film_room.html` |
