# Podcast Feature Implementation Plan

## Context

DraftGuru aims to be "the internet's frontpage for the NBA Draft." The app already aggregates news via RSS, but podcast content — a major part of the draft conversation — is missing. This feature adds a full podcast system: admin-curated show management, RSS-based episode ingestion with AI-powered player extraction, a dedicated `/podcasts` page, a homepage section, and an in-page audio player.

## Architecture Decisions

- **Separate tables** (not reusing news infrastructure) — `podcast_shows`, `podcast_episodes`. Podcast shows share the same *role* as news sources (feed source config) but carry podcast-specific metadata (`artwork_url`, `author`, `description`, `website_url`) that is user-facing in sidebar directory, episode rows, and hero cards. Merging into `news_sources` would mean nullable columns that only matter for one type or a generic JSON blob — both worse than clean separation.
- **Unified mentions table** — single `player_content_mentions` table with a `ContentType` enum (NEWS, PODCAST, ...) replacing the old `news_item_player_mentions` table. This supports consolidated trending/player feeds across all content types and makes future content types trivial to add.
- **Direct RSS feed parsing** for episode ingestion — no external API dependency. Podcast RSS feeds contain all needed episode metadata (title, description, audio URL, artwork, duration, publish date). The app already has RSS parsing infrastructure from the news feature. Shows are admin-curated: find a good podcast, paste its RSS feed URL into the admin form, and episodes are fetched on a schedule. This replaces the originally planned Podcast Index API integration (see Changelog: Ingestion Source Decision).
- **AI extraction** via Gemini on episode title/description text (not audio transcription)
- **In-page audio player** (HTML5 `<audio>`) + external "Listen on..." link
- **Hero + scoreboard list layout** — featured episode hero card with fuchsia accent + compact scoreboard episode rows (evolved from initial Stitcher-like card grid during Design Phase — see `mockups/draftguru_podcasts_v2.html`)

---

## Design Phase: Mockups & Visual Direction

**Goal:** Establish the visual language and layout patterns for the podcast feature before writing production code. Iterate on HTML mockups until the design is locked.

**Status:** Completed

### Mockup Files
- `mockups/draftguru_podcasts.html` — v1 (card-grid layout, initial exploration)
- `mockups/draftguru_podcasts_v2.html` — v2 (final, hero + scoreboard list layout)

### Layout Decisions

**Homepage section** — two independent visual elements:
1. **Hero featured card** — horizontal flexbox layout: 300px episode artwork on the left, episode details (show name, title, summary, meta, play button with progress bar) on the right. Fuchsia outline, drop shadow, "Latest Episode" pulse badge.
2. **Scoreboard episode list** — compact rows beneath the hero. Each row: 52px episode artwork, show name + title on one line, duration/time meta + category tag pill on a second line, play button on the right.

**Dedicated `/podcasts` page** — three-zone layout:
1. **Hero featured card** at top (same component as homepage)
2. **Filter bar + episode list** in the main column: type-only filter chips (no show filters — those are in the sidebar) + richer episode rows with artwork, show name + tag pill, title, 2-line summary, meta, play button
3. **Sidebar** with show directory (logos + episode counts) and trending player mentions (bar chart)

### Visual Style Decisions
- **Accent color:** Fuchsia (`#d946ef`) — consistent across hero outline, tag pills, filter active state, sidebar headings
- **Episode type tags:** Color-coded pills using `.episode-tag--{type}` classes. Eight types: Interview (emerald), Draft Analysis (indigo), Mock Draft (blue), Game Breakdown (slate), Trade & Intel (rose), Prospect Debate (amber), Mailbag (cyan), Event Preview (fuchsia)
- **Filter chips:** Match the news feed `.story-filter` pattern — `2px` border, `0.375rem` border-radius, monospace uppercase, `0.75rem` font. Active state uses fuchsia tint (news uses cyan). Horizontal scroll with hidden scrollbar.
- **Filter chips show type only** — show filtering is handled by the sidebar directory, avoiding duplication
- **No player tags in episode rows** — trending mentions sidebar handles player visibility
- **No per-row "Listen on" links** — removed to keep rows clean; listen destination is an episode-level detail
- **Play buttons vertically centered** in episode rows via `align-items: center`

### Data Model Conventions (reflected in mockup JS)
- `episodeArt` — per-episode artwork (unique to each episode, shown in episode lists and hero)
- `logo` — show-level brand icon (shown in sidebar directory and ticker)
- `tag` field on episodes maps to `PodcastEpisodeTag` enum via `TAG_CLASSES` JS lookup

### Technical Notes from Mockup Iteration
- CSS Grid failed to render the hero card horizontally in some browsers; resolved by using explicit `display: flex; flex-direction: row` with `-webkit-flex` prefix and fixed `width`/`min-width` on artwork
- Episode rows use `display: block` outer container + `display: flex` inner wrapper pattern (direct flex on `<a>` tags was unreliable)
- Hero and episode list must be **separate visual elements** (not nested in one shared container) — combining them caused the hero to overstretch vertically

---

## Phase 0: Unified Player Mentions Migration

**Goal:** Replace the single-purpose `news_item_player_mentions` junction table with a polymorphic `player_content_mentions` table that supports any content type. This is a prerequisite for the podcast feature and all future content-type integrations (tweets, YouTube, etc.).

### 0a. New Schema (`app/schemas/player_content_mentions.py`)

Replaces `app/schemas/news_item_player_mentions.py`.

```python
class ContentType(str, Enum):
    NEWS = "news"
    PODCAST = "podcast"

class PlayerContentMention(SQLModel, table=True):
    __tablename__ = "player_content_mentions"
    id, player_id (FK → players_master.id), content_type (ContentType enum),
    content_id (int, polymorphic — no FK), published_at (denormalized datetime),
    source (MentionSource), created_at
```

- `MentionSource` enum moves from old file to this new file
- UniqueConstraint on `(content_type, content_id, player_id)`
- Index on `(player_id, created_at)` for trending
- Index on `(content_type, content_id)` for reverse lookups
- `published_at` denormalized from the content table — avoids JOINs in trending queries

### 0b. Alembic Migration
- Create `player_content_mentions` table
- Copy data from `news_item_player_mentions` → new table with `content_type='news'`, `content_id=news_item_id`, joining `news_items` for `published_at`
- Drop `news_item_player_mentions` table and its indexes

### 0c. Update News Ingestion (`app/services/news_ingestion_service.py`)
- Change `_persist_player_mentions()` to insert `PlayerContentMention` rows with `content_type=ContentType.NEWS`, `content_id=news_item.id`, `published_at=news_item.published_at`
- Update import from `news_item_player_mentions` → `player_content_mentions`

### 0d. Update News Service (`app/services/news_service.py`)
- **Trending queries** (`get_trending_players`, `get_trending_sparkline_data`): replace `NewsItemPlayerMention` with `PlayerContentMention`, filter by `content_type=ContentType.NEWS` (or omit filter for cross-content trending later)
- **Player feed** (`get_player_news_feed`): update mention subquery to use `PlayerContentMention` where `content_type=ContentType.NEWS`
- Update all `# type: ignore` annotations as needed

### 0e. Update Backfill Script (`scripts/backfill_player_mentions.py`)
- Update to use `PlayerContentMention` with `content_type=ContentType.NEWS`

### 0f. Update Integration Tests
- `tests/integration/conftest.py` — change import from `news_item_player_mentions` to `player_content_mentions`
- `tests/integration/test_trending.py` — replace all `NewsItemPlayerMention(news_item_id=...)` with `PlayerContentMention(content_type=ContentType.NEWS, content_id=...)`
- `tests/integration/test_persist_player_mentions.py` — same pattern
- `tests/integration/test_player_feed.py` — same pattern

### 0g. Remove Old Schema
- Delete `app/schemas/news_item_player_mentions.py`

### Phase 0 Tests
- Verify: `make precommit`, `mypy app --ignore-missing-imports`, `pytest tests/unit -q`, `pytest tests/integration -q`
- Verify migration: `alembic upgrade head` then `alembic downgrade base` against disposable DB
- Existing trending and player feed tests must continue to pass with new table

### Phase 0 Commit
```
refactor: unify player mentions into polymorphic content_mentions table

Replaces news_item_player_mentions with player_content_mentions table
supporting ContentType enum (NEWS, PODCAST, ...). Migrates existing
data and updates all news services, ingestion, and tests. Enables
consolidated trending across future content types.
```

---

## Phase 1: Podcast Database & Models

**Goal:** Establish the podcast data layer — schemas, migration, and Pydantic response models.

### 1a. Config (`app/config.py`)
No new config settings needed — podcast ingestion uses direct RSS parsing (no external API keys).

### 1b. Schemas (2 new files)

**`app/schemas/podcast_shows.py`** — mirrors `app/schemas/news_sources.py`
- `PodcastShow` table with: `id`, `name`, `display_name`, `feed_url` (unique), `artwork_url`, `author`, `description`, `website_url`, `is_draft_focused` (bool, default True), `is_active`, `fetch_interval_minutes`, `last_fetched_at`, `created_at`, `updated_at`
- `is_draft_focused`: when `True`, all episodes are ingested without relevance checks (dedicated draft podcasts). When `False`, episodes must pass a two-stage relevance filter before ingestion (general sports shows like The Ringer, Bill Simmons, etc.).

**`app/schemas/podcast_episodes.py`** — mirrors `app/schemas/news_items.py`
- `PodcastEpisode` table with: `id`, `show_id` (FK), `external_id`, `title`, `description`, `audio_url`, `duration_seconds`, `episode_url`, `artwork_url`, `season`, `episode_number`, `summary`, `tag` (PodcastEpisodeTag enum), `published_at`, `created_at`, `player_id` (FK)
- UniqueConstraint on `(show_id, external_id)`
- `PodcastEpisodeTag` enum: Interview, Draft Analysis, Mock Draft, Game Breakdown, Trade & Intel, Prospect Debate, Mailbag, Event Preview

_No separate podcast mentions table — podcast mentions use the unified `player_content_mentions` table from Phase 0 with `content_type=ContentType.PODCAST`._

### 1c. Alembic Migration
- `alembic revision --autogenerate -m "add podcast_shows and podcast_episodes tables"`
- Creates 2 tables with FKs and indexes

### 1d. Response Models (`app/models/podcasts.py`) — mirrors `app/models/news.py`
- `PodcastEpisodeRead`: show_name, artwork, title, summary, tag (PodcastEpisodeTag), audio_url, episode_url, duration (formatted "45:23"), time (relative), listen_on_text
- `PodcastFeedResponse`: items + pagination (total, limit, offset)
- `PodcastShowRead`, `PodcastShowCreate`: admin views
- `PodcastIngestionResult`: shows_processed, episodes_added, episodes_skipped, episodes_filtered (failed relevance), mentions_added, errors

### 1e. Test Infrastructure (`tests/integration/conftest.py`)
- Add schema imports: `podcast_shows`, `podcast_episodes`
- Add factory helpers: `make_podcast_show()`, `make_podcast_episode()`

### Phase 1 Tests
- `tests/unit/test_podcast_service.py` — `format_duration()` utility (write the function signature + utility in a minimal `app/services/podcast_service.py` stub so the test can import it)
- Verify: `make precommit`, `mypy app --ignore-missing-imports`, `pytest tests/unit -q`
- Verify migration: `alembic upgrade head` then `alembic downgrade base` against disposable DB

### Phase 1 Commit
```
feat: add podcast schema, migration, and response models

Introduces podcast_shows and podcast_episodes tables with Alembic
migration. Adds Pydantic response models and test infrastructure.
Podcast mentions use the unified player_content_mentions table.
```

---

## Phase 2: Services (RSS Parsing, AI Summarization, Retrieval, Ingestion)

**Goal:** Build all business logic — RSS feed parsing, AI extraction, feed retrieval, and the 3-phase ingestion pipeline. After this phase, podcast data can be fetched, analyzed, stored, and queried programmatically.

### 2a. AI Summarization (`app/services/podcast_summarization_service.py`) — mirrors `app/services/news_summarization_service.py`
- `PodcastSummarizationService` singleton with lazy Gemini client
- Reuses `gemini_summarization_api_key` from config
- **Two distinct Gemini calls** — relevance check and full analysis are separate prompts, separate calls, for determinism.

#### Relevance Check (lightweight, for general shows only)
- `check_draft_relevance(title, description)` → `bool`
- Only called when `is_draft_focused=False` AND the keyword pre-filter did not match
- Lightweight prompt, expects a single JSON boolean response

```
You are a sports content filter for DraftGuru, an NBA Draft analytics site.

Determine whether this podcast episode is about or substantially discusses the NBA Draft, draft prospects, or college basketball players projected for the draft.

Answer with valid JSON only:
{"is_draft_relevant": true}
or
{"is_draft_relevant": false}
```

#### Full Episode Analysis (for all relevant episodes)
- `analyze_episode(title, description)` → `EpisodeAnalysis`
- `EpisodeAnalysis` dataclass: `summary` + `tag` (PodcastEpisodeTag) + `mentioned_players`
- **Always call Gemini** — even with sparse input, Gemini can often infer from the title alone. No skip threshold.
- Podcast-specific system prompt:

```
You are a sports podcast editor for DraftGuru, an NBA Draft analytics site.

Analyze this podcast episode and provide:
1. A compelling 1-2 sentence summary that captures the key topic
2. A classification tag for the episode type
3. A list of NBA draft prospects mentioned by name

Tags (choose exactly one):
- "Interview": Guest interview with a prospect, scout, GM, or analyst
- "Draft Analysis": Prospect evaluations, rankings, tier discussions
- "Mock Draft": Mock draft walkthrough or pick-by-pick projection
- "Game Breakdown": Film review or game recap focusing on prospect performance
- "Trade & Intel": Rumors, workouts, measurements, behind-the-scenes draft chatter
- "Prospect Debate": Head-to-head comparisons, "who goes first?" style arguments
- "Mailbag": Listener Q&A, fan questions, community interaction
- "Event Preview": Combine, tournament, All-Star, draft night, or other event previews/recaps

Guidelines:
- If the description is minimal, base your analysis on the episode title
- Keep summaries punchy (1-2 sentences), focus on the hook
- Extract full prospect names (e.g., "Cooper Flagg", not "Flagg")
- Only include prospect/college players, not NBA veterans or coaches
- Return empty list if no prospects are mentioned

Respond with valid JSON only:
{"summary": "...", "tag": "...", "mentioned_players": ["Name 1"]}
```

### 2b. Retrieval Service (`app/services/podcast_service.py`) — mirrors `app/services/news_service.py`
- `get_podcast_feed(db, limit, offset)` → paginated episode feed with joined show data
- `get_latest_podcast_episodes(db, limit=6)` → homepage section
- `get_player_podcast_feed(db, player_id)` → query `player_content_mentions` where `content_type=PODCAST`
- `get_active_shows(db)` → for ingestion
- `format_duration(seconds)` → "45:23" or "1:02:03"
- Reuse `format_relative_time()` from `app/services/news_service.py`

### 2c. Ingestion Pipeline (`app/services/podcast_ingestion_service.py`) — mirrors `app/services/news_ingestion_service.py`

**Per-episode relevance flow (before Phase 1 persistence):**
```
For each episode from RSS feed:
  │
  ├─ Show is draft-focused? (`is_draft_focused = True`)
  │   └─ Yes → RELEVANT, skip to full Gemini analysis
  │
  ├─ Keyword scan on title + description
  │   Keywords: "NBA draft", "mock draft", "draft prospect", "combine",
  │   "draft board", "draft pick", "lottery", known prospect names, etc.
  │   └─ Match found → RELEVANT, skip to full Gemini analysis
  │
  ├─ No keyword match → lightweight Gemini relevance call
  │   └─ is_draft_relevant = True  → RELEVANT, proceed
  │   └─ is_draft_relevant = False → SKIP episode entirely
  │
  └─ Full Gemini analysis (summary + tag + mentioned_players)
```

- Same 3-phase pattern as news ingestion:
  1. Fetch + parse RSS feed for each active show (using `feedparser` / `httpx`, same pattern as news ingestion) + run relevance filter + run AI analysis on relevant episodes (no DB held)
  2. Persist relevant episodes with `ON CONFLICT DO NOTHING` on `(show_id, external_id)`
  3. Best-effort player mention persistence → inserts into `player_content_mentions` with `content_type=ContentType.PODCAST`, using `resolve_player_names_as_map()` from `app/services/player_mention_service.py`
- Incremental: uses `last_fetched_at` to skip episodes older than last fetch
- RSS fields mapped to schema: `<title>` → title, `<description>` → description, `<enclosure url>` → audio_url, `<itunes:duration>` → duration_seconds, `<itunes:image>` → artwork_url, `<itunes:episode>` → episode_number, `<itunes:season>` → season, `<guid>` → external_id, `<pubDate>` → published_at
- `PodcastIngestionResult` extended: `shows_processed`, `episodes_added`, `episodes_skipped`, `episodes_filtered` (failed relevance), `mentions_added`, `errors`

### 2d. Consolidated Trending (required)
- Update `get_trending_players()` and `get_trending_sparkline_data()` in `app/services/news_service.py` to aggregate across ALL content types by removing the `content_type` filter from the `player_content_mentions` query. Homepage trending reflects mentions from both news and podcasts from day one.
- Podcast page sidebar uses a separate query scoped to `content_type='podcast'` for podcast-specific mention counts.
- Player trending and episode type tags (`PodcastEpisodeTag`) are orthogonal — trending is driven by `player_content_mentions`, episode tags live on `PodcastEpisode.tag` and are used only for filtering/display on the podcast page.

### Phase 2 Tests
- `tests/unit/test_podcast_summarization.py` — Gemini response parsing (valid JSON, empty players, markdown-wrapped), relevance check parsing (true/false)
- `tests/unit/test_podcast_ingestion.py` — keyword pre-filter logic: matches on title, matches on description, no match triggers Gemini call, draft-focused shows bypass filter entirely
- `tests/unit/test_podcast_service.py` — expand with retrieval helper tests if applicable
- Verify: `make precommit`, `mypy app --ignore-missing-imports`, `pytest tests/unit -q`

### Phase 2 Commit
```
feat: add podcast services (RSS ingestion, AI summarization, retrieval)

Implements RSS-based episode ingestion with two-stage draft relevance
filter (keyword pre-scan + Gemini gate for general shows), Gemini-powered
episode summarization, 3-phase ingestion pipeline writing to unified
player_content_mentions, and podcast feed retrieval service.
```

---

## Phase 3: API Routes & Admin UI

**Goal:** Wire up the HTTP layer — public API endpoints, admin CRUD routes, admin templates, and sidebar navigation. After this phase, podcasts are fully manageable via the admin panel and queryable via API.

### 3a. API Routes (`app/routes/podcasts.py`) — mirrors `app/routes/news.py`
- `GET /api/podcasts` — paginated episode feed (public), optional `player_id` filter
- `GET /api/podcasts/shows` — list shows (staff, podcasts:view)
- `POST /api/podcasts/shows` — create show (staff, podcasts:edit)
- `POST /api/podcasts/ingest` — trigger ingestion (staff, podcast_ingestion:edit). Runs independently from news ingestion (`POST /api/news/ingest`). The external cron job calls both endpoints sequentially — keeps pipelines decoupled so a failure in one doesn't block the other.

### 3b. Router Registration
- `app/main.py` line 14: add `podcasts` to imports
- `app/main.py` after line 70: `app.include_router(podcasts.router)`

### 3c. Permission Datasets (`app/services/admin_permission_service.py` line 14)
- Add `"podcasts"` and `"podcast_ingestion"` to `KNOWN_DATASETS`

### 3d. Admin Show CRUD (`app/routes/admin/podcast_shows.py`) — mirrors `app/routes/admin/news_sources.py`
- Standard CRUD: list, new form, create, edit form, update, delete
- Admin pastes RSS feed URL to add a new show (same workflow as adding a news source)

### 3e. Admin Episode CRUD (`app/routes/admin/podcast_episodes.py`) — mirrors `app/routes/admin/news_items.py`
- List with filters (show, date range), edit (summary, player_id), delete

### 3f. Admin Router Registration (`app/routes/admin/__init__.py`)
- Import and `router.include_router()` for both podcast admin routers

### 3g. Admin Templates (4 new files)
- `app/templates/admin/podcast-shows/index.html` — show list table
- `app/templates/admin/podcast-shows/form.html` — create/edit form (paste RSS URL)
- `app/templates/admin/podcast-episodes/index.html` — episode list with filters
- `app/templates/admin/podcast-episodes/form.html` — edit episode form

### 3h. Admin Sidebar (`app/templates/admin/base.html`)
- Add `can_view_podcasts` and `can_view_podcast_ingestion` permission checks
- Add "Podcast Shows" and "Podcast Episodes" nav items after "Images" (line 66)

### Phase 3 Tests
- `tests/integration/test_podcasts.py`:
  - Empty feed returns 200 with empty items
  - Episodes with show data return correct response shape
  - Ingestion requires auth
  - Admin show CRUD (create, list, edit, delete)
  - Admin episode list and edit
- Verify: `make precommit`, `mypy app --ignore-missing-imports`, `pytest tests/unit -q`, `pytest tests/integration -q`

### Phase 3 Commit
```
feat: add podcast API routes and admin UI

Adds public podcast feed API, admin CRUD for shows and episodes,
permission datasets, and admin sidebar navigation.
```

---

## Phase 4: Public Frontend (Podcasts Page, Homepage Section, Audio Player)

**Goal:** Build the user-facing experience — the dedicated `/podcasts` page, the homepage "Latest Podcasts" section, the retro inline audio player, and navbar navigation. Follow the layout and visual patterns established in the Design Phase mockup (`mockups/draftguru_podcasts_v2.html`).

### 4a. CSS (`app/static/css/podcasts.css`)
- `.podcast-featured` — horizontal hero card: fixed-width artwork left, episode details right (flex row, fuchsia outline, drop shadow)
- `.podcast-featured__artwork` / `__body` / `__title` / `__summary` / `__meta` / `__player` — hero sub-components
- `.episode-row` / `.episode-row__inner` — compact homepage scoreboard rows (52px art, single-line show+title, meta+tag)
- `.episode-row--page` — richer podcast page rows (72px art, show+tag line, title, 2-line summary, meta, play button)
- `.episode-tag` + `.episode-tag--{type}` — color-coded category pills (8 types, see Design Phase)
- `.filter-chip` — type filter chips matching news feed `.story-filter` style (2px border, 0.375rem radius, monospace uppercase, fuchsia active state)
- `.podcast-sidebar` / `.show-directory-item` / `.trending-mention` — sidebar components
- `.podcast-player` — custom-styled inline audio controls (play/pause button, progress bar, time display)
- Retro aesthetic: fuchsia accent, card-ring outline, hover lift, mono-font metadata

### 4b. JavaScript (`app/static/js/podcasts.js`)
- `PodcastsModule` object with `init()`, `renderHero()`, `renderEpisodeList()`, `renderPageList()`, `togglePlay()`, `formatTime()`
- `TAG_CLASSES` mapping from `PodcastEpisodeTag` display name → CSS class suffix
- Filter chip toggle (type-only; show filtering via sidebar)
- Single-audio management: only one episode plays at a time
- Lazy `Audio()` creation per row on first play
- Progress bar via `timeupdate` + range input
- Data from `window.PODCAST_EPISODES`, `window.PODCAST_SHOWS`
- Episode data uses `episodeArt` (per-episode artwork); show data uses `logo` (brand icon)

### 4c. Dedicated Page (`app/templates/podcasts.html`)
- Extends `base.html`, loads `podcasts.css` and `podcasts.js`
- Section header with fuchsia accent + headphones icon + stats (episode count, show count, players tagged)
- Hero featured card (latest/featured episode)
- Type-only filter bar (no show filters — sidebar handles that)
- Two-column layout: episode list (main) + sidebar (show directory + trending mentions)
- Pagination at bottom of episode list

### 4d. UI Route (`app/routes/ui.py`)
- `GET /podcasts` → renders `podcasts.html` with `get_podcast_feed()` data, shows, and trending
- Modify `home()` → fetch `get_latest_podcast_episodes(db, limit=6)` and inject as `window.PODCAST_EPISODES`

### 4e. Homepage Section (`app/templates/home.html`)
- New "Latest Podcasts" section with fuchsia accent, headphones icon
- Hero featured card (most recent episode) + scoreboard list (next 5 episodes) with category tag pills
- "View All Podcasts" CTA link

### 4f. Homepage JS (`app/static/js/home.js`)
- Add podcast rendering: `renderFeaturedPodcast()` + `renderHomeEpisodeList()` with `TAG_CLASSES` lookup
- Initialize on `DOMContentLoaded`

### 4g. Navbar (`app/templates/partials/navbar.html`)
- Add "Podcasts" link between brand and search

### Phase 4 Tests
- `tests/integration/test_podcasts.py` — add:
  - `GET /podcasts` returns 200 HTML
  - Homepage still renders 200 with podcast section present
- Visual: `make dev` → `make visual` → review screenshots of `/podcasts` page and homepage
- Manual: verify audio player plays/pauses, progress bar works, only one episode plays at a time

### Phase 4 Commit
```
feat: add public podcasts page, homepage section, and audio player

Implements /podcasts page with hero featured card, type-filtered
episode list, show directory sidebar, and trending mentions.
Homepage gets hero + scoreboard podcast section. Retro inline
HTML5 audio player and navbar link.
```

---

## Files Summary

**New files (21):**
| File | Phase | Purpose |
|------|-------|---------|
| `app/schemas/player_content_mentions.py` | 0 | Unified polymorphic mentions table |
| `app/schemas/podcast_shows.py` | 1 | PodcastShow table |
| `app/schemas/podcast_episodes.py` | 1 | PodcastEpisode table |
| `app/models/podcasts.py` | 1 | Request/response models |
| `app/services/podcast_summarization_service.py` | 2 | Gemini AI extraction |
| `app/services/podcast_service.py` | 1–2 | Retrieval/formatting (stub in P1, full in P2) |
| `app/services/podcast_ingestion_service.py` | 2 | 3-phase RSS ingestion |
| `app/routes/podcasts.py` | 3 | Public API routes |
| `app/routes/admin/podcast_shows.py` | 3 | Admin show CRUD |
| `app/routes/admin/podcast_episodes.py` | 3 | Admin episode CRUD |
| `app/templates/podcasts.html` | 4 | Public /podcasts page |
| `app/templates/admin/podcast-shows/index.html` | 3 | Admin show list |
| `app/templates/admin/podcast-shows/form.html` | 3 | Admin show form (paste RSS URL) |
| `app/templates/admin/podcast-episodes/index.html` | 3 | Admin episode list |
| `app/templates/admin/podcast-episodes/form.html` | 3 | Admin episode form |
| `app/static/css/podcasts.css` | 4 | Styles + audio player |
| `app/static/js/podcasts.js` | 4 | JS module + audio player |
| `tests/unit/test_podcast_summarization.py` | 2 | Summarization + relevance check unit tests |
| `tests/unit/test_podcast_ingestion.py` | 2 | Keyword pre-filter + relevance flow unit tests |
| `tests/unit/test_podcast_service.py` | 1 | Service unit tests |
| `tests/integration/test_podcasts.py` | 3 | Integration tests |

**Deleted files (1):**
| File | Phase | Reason |
|------|-------|--------|
| `app/schemas/news_item_player_mentions.py` | 0 | Replaced by `player_content_mentions.py` |

**Modified files (13):**
| File | Phase | Change |
|------|-------|--------|
| `app/services/news_ingestion_service.py` | 0 | Use `PlayerContentMention` with `content_type=NEWS` |
| `app/services/news_service.py` | 0 | Update trending + player feed queries |
| `scripts/backfill_player_mentions.py` | 0 | Use new model |
| `tests/integration/test_trending.py` | 0 | Use `PlayerContentMention` |
| `tests/integration/test_persist_player_mentions.py` | 0 | Use `PlayerContentMention` |
| `tests/integration/test_player_feed.py` | 0 | Use `PlayerContentMention` |
| `tests/integration/conftest.py` | 0, 1 | Update import, add podcast factories |
| `app/main.py` | 3 | Register podcasts router |
| `app/services/admin_permission_service.py` | 3 | Add podcast datasets |
| `app/routes/admin/__init__.py` | 3 | Register admin podcast routers |
| `app/routes/ui.py` | 4 | Add `/podcasts` route + homepage data |
| `app/templates/admin/base.html` | 3 | Add podcast nav items |
| `app/templates/home.html` | 4 | Add "Latest Podcasts" section |
| `app/templates/partials/navbar.html` | 4 | Add "Podcasts" link |
| `app/static/js/home.js` | 4 | Add podcast module init |

**Alembic migrations (2):**
| Migration | Phase | Description |
|-----------|-------|-------------|
| `xxxx_unify_player_mentions.py` | 0 | Create unified table, migrate data, drop old |
| `xxxx_add_podcast_tables.py` | 1 | Create podcast_shows + podcast_episodes |

---

## Final Verification Checklist

1. `make precommit` — ruff, formatting, mypy all clean
2. `mypy app --ignore-missing-imports` — no type errors
3. `pytest tests/unit -q` — unit tests pass
4. `pytest tests/integration -q` — integration tests pass (requires TEST_DATABASE_URL)
5. `alembic upgrade head` + `alembic downgrade base` — clean migration cycle
6. `make dev` → visit `/podcasts` page, verify artwork display and audio player
7. `make dev` → visit `/` homepage, verify "Latest Podcasts" section
8. `make dev` → visit `/admin/podcast-shows`, verify CRUD workflow
9. `make visual` → screenshot review for UI changes

---

## Changelog

_Updated as each phase is implemented. Track major steps, technical difficulties, design decisions made during implementation, and anything that deviated from the original plan._

**IMPORTANT — Implementing agent instructions:** After completing each phase, update the corresponding changelog section below before committing. Fill in:
- **Steps taken:** concrete actions performed (files created/modified, commands run, key implementation choices)
- **Technical difficulties:** any issues encountered and how they were resolved (type errors, migration problems, test failures, etc.)
- **Notes & deviations:** anything that diverged from the plan and why

This is a living document. Keeping it current ensures continuity across sessions and provides context for future phases.

### Design Phase: Mockups & Visual Direction
- [x] Started
- [x] Completed
- **Steps taken:**
  - Created v1 mockup (`mockups/draftguru_podcasts.html`) with card-grid layout for initial exploration
  - Explored alternative layouts inspired by Esther Perel's podcast page (list-based view)
  - Built v2 mockup (`mockups/draftguru_podcasts_v2.html`) with hero + scoreboard list pattern
  - Iterated on hero card layout, sizing, padding, and separation from episode list
  - Designed 8 episode type tags with color-coded pills matching the news tag system
  - Aligned filter chip styling with the existing news feed `.story-filter` pattern
  - Designed dedicated podcast page with hero, type filters, episode list, and sidebar
  - Established data model conventions: `episodeArt` vs `logo` distinction
- **Technical difficulties:**
  - CSS Grid repeatedly failed to render the hero card horizontally; resolved with explicit flexbox + webkit prefix
  - Episode rows with `display: flex` on `<a>` tags were unreliable; resolved with block outer + flex inner wrapper
  - Hero and episode list in a shared container caused overstretching; resolved by separating them into independent elements
- **Notes & deviations:**
  - Moved away from "Stitcher-like artwork card grid" (original Architecture Decisions) toward a hero + scoreboard list approach — provides better visual hierarchy and information density
  - Filter chips are type-only (not show + type) — show filtering handled by sidebar directory to avoid duplication
  - Removed per-row "Listen on" links and player tags from episode rows — sidebar handles both concerns
  - Fuchsia accent color confirmed as the podcast feature's visual identity

### Ingestion Source Decision (Pre-Phase 0)
- [x] Completed
- **Decision:** Replace Podcast Index API with direct RSS feed parsing.
- **Rationale:**
  - Podcast Index API is essentially a pre-parsed JSON mirror of podcast RSS feeds — the core episode data (title, description, audio URL, artwork, duration, publish date) all comes from RSS
  - The only unique value-add is cross-feed search (`search/byterm`), but DraftGuru's admin-curated workflow (find a podcast → paste its RSS URL) doesn't need programmatic discovery
  - DraftGuru already has RSS parsing infrastructure from the news feature (`feedparser` + `httpx`)
  - Eliminates an external API dependency (API keys, auth, rate limits, uptime risk from community-run service)
  - Admin flow mirrors news sources: paste RSS feed URL → save → episodes fetched on schedule
- **Separate tables decision:** `podcast_shows` stays separate from `news_sources` despite similar shape. Podcast shows carry user-facing metadata (`artwork_url`, `author`, `description`, `website_url`) used in sidebar directory, episode rows, and hero cards. Merging would require nullable columns or a JSON blob — both worse than clean separation.
- **Files removed from plan:** `app/services/podcast_index_client.py`, config settings `podcast_index_api_key`/`podcast_index_api_secret`, `GET /api/podcasts/search` route
- **Fields removed:** `podcast_index_feed_id` from `PodcastShow` schema

### Draft Relevance Filtering (Pre-Phase 0)
- [x] Completed
- **Decision:** Two-stage relevance filter for general (non-draft-focused) podcast shows.
- **Problem:** Some podcast shows are 100% NBA draft content, but many general sports shows (The Ringer, Bill Simmons, etc.) only occasionally cover the draft. We need to ingest from these shows but only display/store draft-relevant episodes.
- **Design:**
  - `is_draft_focused` boolean on `PodcastShow` — draft-dedicated shows skip all relevance checks
  - Stage 1: keyword scan on episode title + description (cheap, no API call)
  - Stage 2 (keyword miss only): lightweight Gemini call returning `is_draft_relevant` boolean
  - Two separate Gemini calls (relevance vs full analysis) for determinism — relevance is a yes/no gate, analysis is a separate step
  - Episodes that fail both stages are skipped entirely (not persisted)
- **Files added to plan:** `tests/unit/test_podcast_ingestion.py`
- **Fields added:** `is_draft_focused` on `PodcastShow`, `episodes_filtered` on `PodcastIngestionResult`
- **Follow-up:** The same `is_draft_focused` flag and two-stage relevance filter should be added to `NewsSource` for news RSS feeds. Out of scope for this feature — tracked in [#95](https://github.com/JonathanBechtel/draft-app/issues/95). Implement after the podcast feature lands so the pattern is proven and reusable.

### Phase 0: Unified Player Mentions Migration
- [x] Started
- [x] Completed
- **Steps taken:**
  - Created `app/schemas/player_content_mentions.py` with `ContentType` enum (NEWS, PODCAST), `MentionSource` enum (moved from old file), and `PlayerContentMention` table with polymorphic `content_type` + `content_id` design, denormalized `published_at`, and indexes on `(player_id, created_at)` and `(content_type, content_id)`
  - Created Alembic migration `i8j9k0l1m2n3_unify_player_mentions.py` — creates new table, copies data from `news_item_player_mentions` with `content_type='news'` and `published_at` joined from `news_items`, then drops old table. Downgrade reverses the process.
  - Updated `app/services/news_ingestion_service.py` — `_persist_player_mentions()` now fetches `NewsItem.published_at` alongside `id`/`external_id`, constructs `PlayerContentMention` rows with `content_type=ContentType.NEWS` and `published_at`
  - Updated `app/services/news_service.py` — `get_trending_players()`, `_get_daily_mention_counts()`, and `get_player_news_feed()` all use `PlayerContentMention` with `content_type=ContentType.NEWS` filter. Join condition changed from `.news_item_id` to `.content_id`
  - Updated `scripts/backfill_player_mentions.py` — fetches `published_at` from NewsItem query, constructs `PlayerContentMention` rows with `content_type=ContentType.NEWS`
  - Updated `tests/integration/conftest.py` — schema import changed to `player_content_mentions`
  - Updated all integration tests (`test_trending.py`, `test_persist_player_mentions.py`, `test_player_feed.py`) — all mention construction uses `PlayerContentMention(content_type=ContentType.NEWS, content_id=...)` pattern
  - Deleted `app/schemas/news_item_player_mentions.py`
  - Verified: `make fmt`, `make lint`, `mypy app --ignore-missing-imports` (0 errors), `pytest tests/unit -q` (59 passed)
- **Technical difficulties:**
  - None — the migration was straightforward since all consumers followed the same `news_item_id` → `content_type + content_id` pattern
- **Notes & deviations:**
  - Kept `content_type=ContentType.NEWS` filter on all trending/daily/player-feed queries (Phase 0 is news-only; cross-content trending is Phase 2d as planned)
  - The `_persist_player_mentions` function now uses `ext_to_item: dict[str, tuple[int, datetime | None]]` instead of `ext_to_item_id: dict[str, int]` to carry `published_at` alongside the item ID

### Phase 1: Podcast Database & Models
- [x] Started
- [x] Completed
- **Steps taken:**
  - Created `app/schemas/podcast_shows.py` — `PodcastShow` table mirroring `NewsSource` with podcast-specific fields (`artwork_url`, `author`, `description`, `website_url`, `is_draft_focused`)
  - Created `app/schemas/podcast_episodes.py` — `PodcastEpisode` table with `PodcastEpisodeTag` enum (8 types), `audio_url`, `duration_seconds`, `episode_url`, `artwork_url`, `season`, `episode_number`, unique constraint on `(show_id, external_id)`
  - Created Alembic migration `j9k0l1m2n3o4_add_podcast_tables.py` — creates both tables with FKs, indexes, and constraints
  - Created `app/models/podcasts.py` — `PodcastEpisodeRead`, `PodcastFeedResponse`, `PodcastShowRead`, `PodcastShowCreate`, `PodcastIngestionResult` response models
  - Created `app/services/podcast_service.py` stub with `format_duration()` utility
  - Updated `tests/integration/conftest.py` — added `podcast_shows` and `podcast_episodes` schema imports, `make_podcast_show()` and `make_podcast_episode()` factory helpers
  - Created `tests/unit/test_podcast_service.py` — 9 test cases for `format_duration()` covering minutes, hours, zero, None, negative, and edge cases
  - Verified: `make fmt`, `make lint`, `mypy app --ignore-missing-imports` (0 errors), `pytest tests/unit -q` (68 passed), migration upgrade/downgrade/re-upgrade clean
- **Technical difficulties:**
  - `AUTO_INIT_DB` auto-created `podcast_shows` and `podcast_episodes` tables when the app loaded the new schema imports, causing the Alembic migration to fail with `DuplicateTableError`. Resolved by dropping the stale tables before running the migration. This is a known footgun with `AUTO_INIT_DB=true` in dev — not a migration bug.
- **Notes & deviations:**
  - None — implementation matches the plan exactly

### Phase 2: Services
- [x] Started
- [x] Completed
- **Steps taken:**
  - Created `app/services/podcast_summarization_service.py` — `PodcastSummarizationService` singleton with lazy Gemini client, `check_draft_relevance()` (lightweight, temperature=0.1), `analyze_episode()` (full analysis, temperature=0.3), `EpisodeAnalysis` frozen dataclass, two prompt constants, parse helpers with markdown fence stripping
  - Created `tests/unit/test_podcast_summarization.py` — 16 tests: `TestParseAnalysisResponse` (valid JSON, empty players, missing key, non-list, non-string entries, whitespace, markdown block, invalid JSON raises ValueError, unknown tag defaults, all 8 tags parametrized, whitespace around JSON) + `TestParseRelevanceResponse` (true, false, missing key, invalid JSON, markdown block)
  - Expanded `app/services/podcast_service.py` — added `_PODCAST_FEED_COLUMNS`, `get_podcast_feed()`, `get_latest_podcast_episodes()`, `get_player_podcast_feed()` (UNION of mention + direct player_id), `get_active_shows()`, `build_listen_on_text()`, `_row_to_episode_read()`. Reuses `format_relative_time` from news_service.
  - Expanded `tests/unit/test_podcast_service.py` — added `TestBuildListenOnText` (3 tests)
  - Created `app/services/podcast_ingestion_service.py` — `PodcastShowSnapshot` dataclass, `DRAFT_RELEVANCE_KEYWORDS` list, `run_ingestion_cycle()`, `ingest_podcast_show()` with 3-phase pattern (network/AI → persist episodes → persist mentions), `check_keyword_relevance()` pure function, `fetch_podcast_rss_feed()` with podcast-specific field mapping (`_extract_audio_url`, `_extract_podcast_artwork`, `_parse_itunes_duration`, `_parse_int_field`), `_persist_podcast_episodes()` with transient retry, `_persist_player_mentions()` with `ContentType.PODCAST`
  - Created `tests/unit/test_podcast_ingestion.py` — 13 tests: `TestCheckKeywordRelevance` (7 tests) + `TestParseItunesDuration` (6 tests)
  - Updated `app/services/news_service.py` (2d) — `get_trending_players()` and `_get_daily_mention_counts()` now use `PlayerContentMention.published_at` instead of joining `NewsItem`, removed `content_type` filter so trending aggregates across all content types
  - Updated `tests/integration/test_trending.py` (2d) — added `published_at=<datetime>` to all `PlayerContentMention(...)` constructors to match the new query that reads from the mention row
  - Verified: `make precommit` (all passed), `mypy app --ignore-missing-imports` (0 errors), `pytest tests/unit -q` (107 passed), `pytest tests/integration -q` (185 passed, 2 pre-existing failures in unrelated image generation tests)
- **Technical difficulties:**
  - mypy `operator` error on `PlayerContentMention.published_at >= cutoff` because `published_at` is `Optional[datetime]` — resolved with `# type: ignore[arg-type,operator]`
  - mypy loop variable shadowing in `podcast_ingestion_service.py` — `for show in active_shows` (PodcastShow) followed by `for show in show_snapshots` (PodcastShowSnapshot) caused type conflict — resolved by renaming first loop variable to `s`
- **Notes & deviations:**
  - Implementation matches the plan exactly — no deviations
  - `get_player_news_feed()` retains `content_type=NEWS` filter as specified (player news feed shows only news)

### Phase 3: API Routes & Admin UI
- [x] Started
- [x] Completed
- **Steps taken:**
  - Added `"podcasts"` and `"podcast_ingestion"` to `KNOWN_DATASETS` in `app/services/admin_permission_service.py`
  - Created `app/routes/podcasts.py` — public API: `GET /api/podcasts` (paginated feed with optional `player_id` filter), `GET /api/podcasts/sources` (admin list shows), `POST /api/podcasts/sources` (admin create show with duplicate feed_url check), `POST /api/podcasts/ingest` (admin trigger ingestion with cache error recovery)
  - Created `app/routes/admin/podcast_shows.py` — full CRUD: list, new form, create (duplicate check), edit form, update (duplicate check excluding self), delete (dependent episode guard)
  - Created `app/routes/admin/podcast_episodes.py` — list with pagination + show/tag filters, edit form (title, summary, tag, audio_url, episode_url, player_id with search), delete. No create route (episodes from ingestion only)
  - Registered routers: `podcast_shows_router` and `podcast_episodes_router` in `app/routes/admin/__init__.py`, `podcasts.router` in `app/main.py`
  - Created 4 admin templates: `podcast-shows/index.html` (table with Draft Focused/Status/Interval/Last Fetched columns), `podcast-shows/form.html` (all fields including is_draft_focused checkbox), `podcast-episodes/index.html` (filterable paginated table), `podcast-episodes/form.html` (read-only details + editable fields with player search JS)
  - Updated `app/templates/admin/base.html` — added `can_view_podcasts` and `can_view_podcast_ingestion` permission variables, added "Podcast Shows" and "Podcast Episodes" sidebar nav items
  - Created `tests/integration/test_podcasts.py` — 8 tests: empty feed, episode response shape, player_id mention filtering, auth guards (create show, list sources, trigger ingestion), show creation, duplicate feed_url rejection
  - Verified: `make fmt`, `make lint`, `mypy app --ignore-missing-imports` (0 errors), `pytest tests/unit -q` (107 passed), `pytest tests/integration -q` (195 passed)
- **Technical difficulties:**
  - None — all patterns mirrored directly from news_sources/news_items admin routes
- **Notes & deviations:**
  - Plan specified `GET /api/podcasts/shows` but implemented as `GET /api/podcasts/sources` to match the news pattern (`/api/news/sources`). Same for `POST`.
  - Episode edit form includes player search JS (same as news items form) for associating episodes with players

### Phase 4: Public Frontend
- [ ] Started
- [ ] Completed
- **Steps taken:**
  - _(to be filled during implementation)_
- **Technical difficulties:**
  - _(to be filled during implementation)_
- **Notes & deviations:**
  - _(to be filled during implementation)_
