# DraftGuru App Development Roadmap

**Model:** Claude Opus 4.5
**Date:** 2025-11-26
**Branch:** `docs/app-development-plan`

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Current State Analysis](#current-state-analysis)
3. [Architecture Overview](#architecture-overview)
4. [Feature Breakdown](#feature-breakdown)
5. [Database & Schema Considerations](#database--schema-considerations)
6. [Testing Strategy](#testing-strategy)
7. [Integration Points & Placeholders](#integration-points--placeholders)
8. [Implementation Phases](#implementation-phases)
9. [File Organization](#file-organization)
10. [CSS Architecture](#css-architecture)

---

## Executive Summary

This roadmap details the implementation of DraftGuru's public-facing NBA Draft analytics application based on the mockups in `/mockups/`. The app will transform the existing minimal scaffold into a full-featured draft analytics platform with:

- **Homepage** with consensus mock draft, market moves ticker, top prospects grid, VS Arena comparison tool, and live draft buzz feed
- **Player Detail Pages** with bios, analytics dashboards, percentile visualizations, player comparisons, and news feeds
- **Shareable components** designed for eventual PNG export

### Guiding Principles

Per `CLAUDE.md` and the design documents:
- **Fast, clean, lightweight** - No frontend frameworks, plain vanilla HTML/CSS/JS only
- **Retro-analytics aesthetic** - Per `docs/style_guide.md`
- **Simple backend** - FastAPI + Jinja2 templates, thin routes, logic in services
- **Offline analytics** - All percentiles, z-scores, and similarity scores pre-computed in DB
- **Image-ready design** - Build with placeholder images; real images added later
- **Clean slate UI** - Current frontend code is throwaway; replace entirely with mockup-based implementation
- **Feature-flagged sections** - All major sections toggleable via config; hide incomplete or legally-pending features at launch

---

## Current State Analysis

### What Exists

| Component | Status | Notes |
|-----------|--------|-------|
| Database schemas | **Complete** | `players_master`, `combine_*`, `metrics`, `player_similarity` tables populated |
| API routes | **Minimal** | Basic `/players` CRUD, single UI route |
| Templates | **Throwaway** | Current `base.html` is a demo scaffold - discard and rebuild |
| Static assets | **Throwaway** | Current `main.css`/`main.js` are minimal stubs - replace entirely |
| Mockups | **Complete** | Full homepage and player page HTML/CSS/JS - use as implementation source |

> **Important:** The current frontend (`app/templates/base.html`, `app/static/main.css`, `app/static/main.js`) is placeholder demo code. It should be **replaced entirely** with new implementations based on the mockups. Do not attempt to incrementally modify the existing UI.

### Database Tables Available

From `app/schemas/`:

1. **`players_master`** - Core player identity (name, birthdate, college, draft info)
2. **`combine_anthro`** - Anthropometric measurements (height, weight, wingspan, etc.)
3. **`combine_agility`** - Athletic testing (lane agility, sprint, verticals, bench)
4. **`combine_shooting`** - Shooting drill results (spot-up, off-dribble, etc.)
5. **`metric_definitions`** - Catalog of all metrics with display names
6. **`metric_snapshots`** - Versioned metric computation batches
7. **`player_metric_values`** - Per-player percentiles, ranks, z-scores
8. **`player_similarity`** - Pre-computed nearest-neighbor relationships

### What's Missing for MVP

1. **UI Templates** - Home page, player detail page
2. **API Endpoints** - Consensus mock, top prospects, comparisons, news feed
3. **Service Layer** - Business logic for aggregating player data
4. **CSS System** - Full design token implementation from mockups
5. **JS Modules** - Client-side interactivity (ticker, tabs, comparisons)

---

## Architecture Overview

### Application Layers

```
┌─────────────────────────────────────────────────────────────────┐
│                      PRESENTATION LAYER                        │
│  ┌────────────────┐  ┌────────────────┐  ┌──────────────────┐  │
│  │  Jinja2        │  │  Static CSS    │  │  Vanilla JS      │  │
│  │  Templates     │  │  (main.css +   │  │  (main.js +      │  │
│  │                │  │   page-*.css)  │  │   page-*.js)     │  │
│  └────────────────┘  └────────────────┘  └──────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│                        ROUTE LAYER                              │
│  ┌────────────────────────┐  ┌───────────────────────────────┐ │
│  │  UI Routes             │  │  API Routes (JSON)            │ │
│  │  - GET /               │  │  - GET /api/v1/prospects      │ │
│  │  - GET /player/{id}    │  │  - GET /api/v1/mock-draft     │ │
│  │  - GET /compare        │  │  - GET /api/v1/similarity     │ │
│  └────────────────────────┘  └───────────────────────────────┘ │
├─────────────────────────────────────────────────────────────────┤
│                       SERVICE LAYER                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │ PlayerService│  │MetricsService│  │ SimilarityService    │  │
│  │              │  │              │  │                      │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│                        DATA LAYER                               │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  SQLModel Tables (PostgreSQL via asyncpg)                │  │
│  │  players_master | combine_* | metric_* | player_similarity│  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Route Patterns

Per `CLAUDE.md`, routes should be thin. All routes delegate to services:

```python
# UI Route Pattern
@router.get("/player/{player_id}", response_class=HTMLResponse)
async def player_detail(
    request: Request,
    player_id: int,
    db: AsyncSession = Depends(get_session)
):
    player = await player_service.get_player_detail(db, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    return request.app.state.templates.TemplateResponse(
        "player_detail.html",
        {"request": request, "player": player}
    )

# API Route Pattern
@router.get("/api/v1/prospects", response_model=list[ProspectSummary])
async def list_prospects(
    db: AsyncSession = Depends(get_session),
    limit: int = 10,
    offset: int = 0,
):
    return await player_service.list_top_prospects(db, limit, offset)
```

---

## Feature Breakdown

### Homepage Features

Based on `mockups/draftguru_homepage.html`:

#### 1. Market Moves Ticker

| Aspect | Details |
|--------|---------|
| **Purpose** | Scrolling display of risers/fallers |
| **Data Source** | `player_metric_values` delta between snapshots |
| **API Endpoint** | `GET /api/v1/market-moves` |
| **Template** | Partial: `_ticker.html` |
| **JS Module** | `ticker.js` - Infinite scroll animation |

**Implementation Notes:**
- Pre-compute rank changes between `is_current` snapshots
- Return top N risers/fallers ordered by absolute change magnitude
- Placeholder: Mock data until consensus rank tracking implemented

#### 2. Consensus Mock Draft Table

| Aspect | Details |
|--------|---------|
| **Purpose** | Aggregated mock draft from multiple sources |
| **Data Source** | New table required: `consensus_ranks` |
| **API Endpoint** | `GET /api/v1/mock-draft` |
| **Template** | Partial: `_mock_table.html` |
| **JS Module** | None (static table, server-rendered) |

**Schema Addition Required:**
```python
class ConsensusRank(SQLModel, table=True):
    __tablename__ = "consensus_ranks"

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)
    season_id: int = Field(foreign_key="seasons.id", index=True)
    rank: int = Field(description="Consensus position 1-60+")
    average_rank: float = Field(description="Mean from all sources")
    rank_change: int = Field(default=0, description="Change since last update")
    source_count: int = Field(description="Number of mocks averaged")
    calculated_at: datetime = Field(default_factory=datetime.utcnow)
```

**Placeholder Strategy:**
- Seed initial consensus data from existing `player_metric_values` ranks
- Future: Ingest from external mock draft APIs

#### 3. Top Prospects Grid

| Aspect | Details |
|--------|---------|
| **Purpose** | Visual cards for top draft prospects |
| **Data Source** | Join of `players_master` + `combine_anthro` + `consensus_ranks` |
| **API Endpoint** | `GET /api/v1/prospects?limit=6` |
| **Template** | Partial: `_prospect_card.html` |
| **JS Module** | None (hover effects in CSS) |

**Image Placeholder Strategy:**
- Use `placehold.co` URLs with player names
- Template: `https://placehold.co/320x420/edf2f7/1f2937?text={{ player.display_name|urlencode }}`
- Future: Add `image_url` column to `players_master` or separate `player_images` table

#### 4. VS Arena (Head-to-Head Comparison)

| Aspect | Details |
|--------|---------|
| **Purpose** | Interactive player-vs-player metric comparison |
| **Data Source** | `player_metric_values` + `player_similarity` |
| **API Endpoint** | `GET /api/v1/compare?player_a={id}&player_b={id}&category={cat}` |
| **Template** | Partial: `_vs_arena.html` |
| **JS Module** | `comparison.js` - Tab switching, player selection, table rendering |

**Category Tabs:**
- Anthropometrics
- Combine Performance
- Advanced Stats

**Similarity Badge:**
- Pull from `player_similarity` table where dimension matches category

#### 5. Live Draft Buzz Feed

| Aspect | Details |
|--------|---------|
| **Purpose** | News/updates feed for draft coverage |
| **Data Source** | New table required: `news_items` |
| **API Endpoint** | `GET /api/v1/news?limit=10` |
| **Template** | Partial: `_feed_item.html` |
| **JS Module** | Optional: Polling for real-time updates |

**Schema Addition Required:**
```python
class NewsItem(SQLModel, table=True):
    __tablename__ = "news_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(index=True)
    source: str = Field(description="Publication/author name")
    source_url: Optional[str] = Field(default=None)
    player_id: Optional[int] = Field(default=None, foreign_key="players_master.id")
    tag: Optional[str] = Field(default=None, description="riser|faller|analysis|highlight")
    published_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)
```

**Placeholder Strategy:**
- Seed with static mock data
- Future: RSS ingestion service for @DraftExpress, The Ringer, etc.

#### 6. Draft Position Specials (Affiliate Banner)

| Aspect | Details |
|--------|---------|
| **Purpose** | Sportsbook affiliate integration |
| **Data Source** | Static or config-driven |
| **Template** | Partial: `_specials_banner.html` |
| **JS Module** | None |
| **Default Visibility** | **HIDDEN** (`FEATURE_AFFILIATE_SPECIALS=False`) |

> **Launch Note:** This feature requires legal review and affiliate partnership agreements before enabling. Build the component but keep hidden until business requirements are met.

**Placeholder Strategy:**
- Hardcode sample odds in template
- Wrap in feature flag check (`{% if settings.FEATURE_AFFILIATE_SPECIALS %}`)
- Future: External affiliate API or admin-managed content

---

### Player Detail Page Features

Based on `mockups/draftguru_player.html`:

#### 1. Player Bio Section

| Aspect | Details |
|--------|---------|
| **Data Source** | `players_master` + `combine_anthro` |
| **Template** | `player_detail.html` -> `_player_bio.html` |

**Fields Displayed:**
- Photo (placeholder strategy same as prospects)
- Position, College, Height, Weight
- Age (computed from birthdate)
- Class (Freshman/Sophomore/etc.)
- Hometown (`birth_city, birth_state_province`)
- Wingspan

#### 2. Draft Analytics Dashboard (Scoreboard)

| Aspect | Details |
|--------|---------|
| **Purpose** | At-a-glance metrics with retro scoreboard aesthetic |
| **Data Source** | `consensus_ranks` + computed metrics |
| **Template** | Partial: `_scoreboard.html` |

**Metrics Displayed:**
- Consensus Mock Position (with change indicator)
- Draft Buzz Score (volume metric - placeholder)
- True Draft Position (uncertainty range - placeholder)
- Expected Wins Added (projection - placeholder)
- 7-Day Trend (computed from historical ranks)

**Placeholder Strategy:**
Most scoreboard metrics require future data pipelines:
- `buzz_score`: Placeholder 0-100 random or null
- `true_position`: Use consensus_rank with ±0.5 range
- `wins_added`: Null/placeholder until projection model exists

#### 3. Performance Section (Percentile Bars)

| Aspect | Details |
|--------|---------|
| **Purpose** | Visual percentile rankings vs cohorts |
| **Data Source** | `player_metric_values` |
| **API Endpoint** | `GET /api/v1/player/{id}/metrics?category={cat}&cohort={type}` |
| **Template** | Partial: `_percentile_bars.html` |
| **JS Module** | `performance.js` - Tab/cohort switching |

**Controls:**
- Cohort selector (Current Draft, All-Time Draft, Current NBA, All-Time NBA)
- Position filter toggle
- Category tabs (Anthropometrics, Combine Performance, Advanced Stats)

**Data Flow:**
```
User selects cohort/position ->
JS fetches /api/v1/player/{id}/metrics?cohort=X&position_filter=Y&category=Z ->
API queries player_metric_values with snapshot matching cohort/position scope ->
Returns metric definitions + values + percentiles ->
JS updates DOM with animated bars
```

#### 4. Player Comparisons Section

| Aspect | Details |
|--------|---------|
| **Purpose** | Grid of similar players with "Compare" action |
| **Data Source** | `player_similarity` |
| **API Endpoint** | `GET /api/v1/player/{id}/similar?dimension={dim}&limit=8` |
| **Template** | Partial: `_comparison_grid.html` |
| **JS Module** | `comparisons.js` - Modal handling, tab switching |

**Similarity Dimensions:**
- `anthro` (Anthropometrics)
- `combine` (Combine Performance)
- `composite` (Overall)

**Compare Modal:**
- Click "Compare" -> Opens modal with side-by-side metrics
- Reuses `_vs_arena.html` pattern

#### 5. Head-to-Head Section

Same as homepage VS Arena but with current player pre-selected as Player A.

#### 6. Player News Feed

Same structure as homepage but filtered to `player_id`.

---

## Database & Schema Considerations

### New Tables Required

#### 1. `consensus_ranks`
Aggregated mock draft positions from multiple sources.

```python
# app/schemas/consensus.py
class ConsensusRank(SQLModel, table=True):
    __tablename__ = "consensus_ranks"
    __table_args__ = (
        UniqueConstraint("player_id", "season_id", name="uq_consensus_player_season"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)
    season_id: int = Field(foreign_key="seasons.id", index=True)
    rank: int
    average_rank: float
    rank_change: int = Field(default=0)
    source_count: int
    high_rank: Optional[int] = Field(default=None)
    low_rank: Optional[int] = Field(default=None)
    calculated_at: datetime = Field(default_factory=datetime.utcnow)
```

#### 2. `news_items`
Draft-related news and updates.

```python
# app/schemas/news.py
class NewsItem(SQLModel, table=True):
    __tablename__ = "news_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    source: str
    source_url: Optional[str] = Field(default=None)
    player_id: Optional[int] = Field(default=None, foreign_key="players_master.id")
    tag: Optional[str] = Field(default=None)  # riser|faller|analysis|highlight
    summary: Optional[str] = Field(default=None)
    published_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)
```

### Schema Enhancements

#### `players_master` Additions

```python
# Add to existing schema
image_url: Optional[str] = Field(default=None, description="Player headshot URL")
slug: Optional[str] = Field(default=None, index=True, description="URL-friendly identifier")
```

### Migration Plan

1. Create migration for `consensus_ranks` table
2. Create migration for `news_items` table
3. Add optional columns to `players_master`
4. Seed initial data:
   - Generate placeholder consensus ranks from existing combine data
   - Create sample news items for testing

---

## Testing Strategy

Per `CLAUDE.md`, we favor integration tests that exercise the full stack.

### Test Categories

#### 1. Integration Tests (`tests/integration/`)

**Setup:**
- Use `TEST_DATABASE_URL` pointing to disposable Postgres
- Set `PYTEST_ALLOW_DB=1`
- Fixtures create/drop tables per test

**Route Tests:**
```python
# tests/integration/test_homepage.py
@pytest.mark.asyncio
async def test_homepage_renders(app_client):
    """Homepage should return 200 with expected sections."""
    response = await app_client.get("/")
    assert response.status_code == 200
    assert "Consensus Mock Draft" in response.text
    assert "Top Prospects" in response.text

@pytest.mark.asyncio
async def test_homepage_ticker_data(app_client, db_session):
    """Ticker should display market moves from database."""
    # Seed test data
    await seed_market_moves(db_session)

    response = await app_client.get("/")
    assert "▲" in response.text or "▼" in response.text
```

**API Tests:**
```python
# tests/integration/test_api_prospects.py
@pytest.mark.asyncio
async def test_list_prospects(app_client, db_session):
    """API should return top prospects with required fields."""
    await seed_prospects(db_session, count=10)

    response = await app_client.get("/api/v1/prospects?limit=5")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 5
    assert all("display_name" in p for p in data)
    assert all("consensus_rank" in p for p in data)

@pytest.mark.asyncio
async def test_player_metrics(app_client, db_session):
    """Player metrics endpoint should return percentiles."""
    player = await seed_player_with_metrics(db_session)

    response = await app_client.get(
        f"/api/v1/player/{player.id}/metrics?category=anthropometrics"
    )
    assert response.status_code == 200
    data = response.json()
    assert "metrics" in data
    assert all("percentile" in m for m in data["metrics"])
```

#### 2. Unit Tests (`tests/unit/`)

For pure logic that doesn't require database:

```python
# tests/unit/test_services.py
def test_percentile_tier_classification():
    """Percentile values should map to correct tiers."""
    from app.services.metrics import get_percentile_tier

    assert get_percentile_tier(95) == "elite"
    assert get_percentile_tier(75) == "good"
    assert get_percentile_tier(50) == "average"
    assert get_percentile_tier(25) == "below-average"

def test_similarity_badge_class():
    """Similarity scores should map to correct CSS classes."""
    from app.services.similarity import get_similarity_badge_class

    assert get_similarity_badge_class(92) == "similarity-badge-high"
    assert get_similarity_badge_class(80) == "similarity-badge-good"
    assert get_similarity_badge_class(65) == "similarity-badge-moderate"
    assert get_similarity_badge_class(50) == "similarity-badge-weak"
```

### Test Data Factories

```python
# tests/factories.py
from app.schemas.players_master import PlayerMaster
from app.schemas.consensus import ConsensusRank

async def create_test_player(db: AsyncSession, **overrides) -> PlayerMaster:
    """Create a player with sensible defaults."""
    defaults = {
        "first_name": "Test",
        "last_name": "Player",
        "display_name": "Test Player",
        "school": "Test University",
        "draft_year": 2025,
    }
    defaults.update(overrides)
    player = PlayerMaster(**defaults)
    db.add(player)
    await db.flush()
    await db.refresh(player)
    return player

async def create_consensus_rank(db: AsyncSession, player_id: int, **overrides):
    """Create consensus rank for a player."""
    defaults = {
        "player_id": player_id,
        "season_id": 1,  # Assumes seeded
        "rank": 1,
        "average_rank": 1.0,
        "source_count": 10,
    }
    defaults.update(overrides)
    rank = ConsensusRank(**defaults)
    db.add(rank)
    await db.flush()
    return rank
```

### Running Tests

```bash
# Integration tests (requires database)
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/draft_test \
PYTEST_ALLOW_DB=1 \
pytest tests/integration -q

# Unit tests (no database needed)
pytest tests/unit -q

# All tests with coverage
pytest --cov=app --cov-report=term-missing
```

---

## Integration Points & Placeholders

### External Data Sources (Future)

| Integration | Purpose | Placeholder Strategy |
|-------------|---------|---------------------|
| Mock Draft APIs | Consensus ranking data | Seed from existing combine data |
| NBA Stats API | Player statistics | Use existing combine tables |
| RSS Feeds | News aggregation | Static seed data |
| Image CDN | Player headshots | `placehold.co` URLs |
| Sportsbook APIs | Live odds | Hardcoded sample data |

### Placeholder Implementation Pattern

```python
# app/services/external.py

class ExternalDataService:
    """
    Handles external data that may not be available yet.
    Returns placeholder data when integrations aren't configured.
    """

    async def get_draft_odds(self, player_id: int) -> Optional[dict]:
        """
        Get sportsbook odds for player's draft position.

        Returns placeholder until sportsbook integration is configured.
        """
        if settings.SPORTSBOOK_API_KEY:
            return await self._fetch_live_odds(player_id)

        # Placeholder: Generate mock odds
        return {
            "top_1": "+150",
            "top_5": "-200",
            "top_10": "-500",
            "source": "placeholder",
        }

    async def get_buzz_score(self, player_id: int) -> int:
        """
        Get social media/news buzz score.

        Returns placeholder until sentiment analysis is configured.
        """
        if settings.BUZZ_SERVICE_URL:
            return await self._fetch_buzz_score(player_id)

        # Placeholder: Random score
        import random
        return random.randint(60, 99)
```

### Configuration for Placeholders

```python
# app/config.py

class Settings(BaseSettings):
    # ... existing settings ...

    # Feature flags for external integrations
    SPORTSBOOK_API_KEY: Optional[str] = None
    BUZZ_SERVICE_URL: Optional[str] = None
    NEWS_RSS_FEEDS: list[str] = []
    IMAGE_CDN_BASE_URL: str = "https://placehold.co"

    # Placeholder mode (for development/demo)
    USE_PLACEHOLDER_DATA: bool = True
```

### Feature Visibility System

Some features may not be ready for launch due to:
- **Legal/business hurdles** (affiliate partnerships, gambling disclaimers)
- **Data dependencies** (consensus rankings, news feeds)
- **Incomplete integrations** (sportsbook APIs, RSS ingestion)

Implement a **feature flag system** to easily show/hide sections:

```python
# app/config.py

class Settings(BaseSettings):
    # ... existing settings ...

    # ══════════════════════════════════════════════════════════════════
    # FEATURE VISIBILITY FLAGS
    # Set to False to hide entire sections from the UI
    # ══════════════════════════════════════════════════════════════════

    # Homepage sections
    FEATURE_MARKET_TICKER: bool = True       # Market Moves ticker
    FEATURE_CONSENSUS_MOCK: bool = True      # Consensus Mock Draft table
    FEATURE_TOP_PROSPECTS: bool = True       # Top Prospects grid
    FEATURE_VS_ARENA: bool = True            # VS Arena comparison tool
    FEATURE_NEWS_FEED: bool = True           # Live Draft Buzz feed
    FEATURE_AFFILIATE_SPECIALS: bool = False # Draft Position Specials (affiliate)

    # Player page sections
    FEATURE_PLAYER_SCOREBOARD: bool = True   # Analytics Dashboard
    FEATURE_PLAYER_PERCENTILES: bool = True  # Performance percentile bars
    FEATURE_PLAYER_COMPARISONS: bool = True  # Similar players grid
    FEATURE_PLAYER_H2H: bool = True          # Head-to-head comparison
    FEATURE_PLAYER_NEWS: bool = True         # Player-specific news

    # Global features
    FEATURE_SEARCH: bool = True              # Search functionality
    FEATURE_SHARE_CARDS: bool = False        # PNG share card generation
```

**Template Usage:**

```jinja2
{# home.html #}

{% if settings.FEATURE_MARKET_TICKER %}
  {% include "partials/_ticker.html" %}
{% endif %}

{% if settings.FEATURE_CONSENSUS_MOCK %}
  {% include "partials/_mock_table.html" %}
{% endif %}

{# Affiliate section - hidden by default until legal ready #}
{% if settings.FEATURE_AFFILIATE_SPECIALS %}
  {% include "partials/_specials_banner.html" %}
{% endif %}
```

**Route-Level Context:**

```python
# app/routes/ui.py

@router.get("/", response_class=HTMLResponse)
async def home(request: Request, db: AsyncSession = Depends(get_session)):
    # Pass feature flags to template context
    return request.app.state.templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "settings": settings,  # Makes all FEATURE_* flags available
            # ... other context
        }
    )
```

**Benefits:**
- Toggle features via environment variables without code changes
- Hide incomplete features in production while developing in staging
- Gradually roll out features as business/legal requirements are met
- Easy A/B testing potential

### Features Likely Hidden at Launch

| Feature | Reason | Launch Hidden |
|---------|--------|---------------|
| Affiliate Specials | Legal/partnership requirements | **Yes** |
| Share Cards (PNG) | Requires additional implementation | **Yes** |
| News Feed | Needs RSS ingestion pipeline | Maybe |
| Market Ticker | Needs historical rank data | Maybe |
| Buzz Score | Needs sentiment service | **Yes** (show placeholder or hide)

---

## Implementation Phases

### Phase 1: Foundation (Core Infrastructure)

**Goal:** Replace throwaway UI with production CSS system, template structure, and routing.

**Tasks:**

1. **Delete Existing UI Code**
   - Remove current `app/templates/base.html` (demo form)
   - Remove current `app/static/main.css` (minimal stub)
   - Remove current `app/static/main.js` (demo script)

2. **CSS System Setup (Plain Vanilla CSS)**
   - Extract design tokens from mockups into new `main.css`
   - Create component classes (cards, buttons, tables, badges)
   - Create utility classes (spacing, typography)
   - No preprocessors, no build step - plain `.css` files only

3. **Template Structure (Jinja2)**
   - Create new `base.html` with navbar, footer, blocks (from mockups)
   - Create `home.html` extending base
   - Create `player_detail.html` extending base
   - Create reusable partials in `partials/` directory

4. **JavaScript Setup (Plain Vanilla JS)**
   - Create new `main.js` with global utilities
   - No frameworks, no build step - plain `.js` files only
   - Use ES6 modules via `<script type="module">` if needed

5. **Route Setup**
   - Refactor `ui.py` with homepage route
   - Add player detail route
   - Create API router in `app/routes/api.py`

6. **Service Layer Foundation**
   - Create `app/services/player.py`
   - Create `app/services/metrics.py`

**Testing Focus:** Route rendering returns 200, templates extend base correctly.

### Phase 2: Homepage Features

**Goal:** Implement all homepage sections with real database queries.

**Tasks:**

1. **Consensus Mock Draft**
   - Create `consensus_ranks` table migration
   - Seed placeholder data
   - Implement `GET /api/v1/mock-draft`
   - Create `_mock_table.html` partial

2. **Top Prospects Grid**
   - Implement `GET /api/v1/prospects`
   - Create `_prospect_card.html` partial
   - Wire up to homepage template

3. **Market Moves Ticker**
   - Compute rank changes in service
   - Create `ticker.js` animation module
   - Create `_ticker.html` partial

4. **VS Arena**
   - Create `comparison.js` module
   - Implement `GET /api/v1/compare`
   - Create `_vs_arena.html` partial

5. **News Feed**
   - Create `news_items` table migration
   - Seed sample data
   - Implement `GET /api/v1/news`
   - Create `_feed_item.html` partial

**Testing Focus:** API endpoints return correct data, homepage displays all sections.

### Phase 3: Player Detail Page

**Goal:** Complete player page with all sections functional.

**Tasks:**

1. **Bio Section**
   - Query player with related data
   - Create `_player_bio.html` partial

2. **Analytics Dashboard**
   - Compute/retrieve scoreboard metrics
   - Create `_scoreboard.html` partial

3. **Performance Section**
   - Implement `GET /api/v1/player/{id}/metrics`
   - Create `performance.js` module
   - Create `_percentile_bars.html` partial

4. **Player Comparisons**
   - Query `player_similarity` table
   - Create `comparisons.js` module
   - Create `_comparison_grid.html` partial

5. **Player News**
   - Filter news by player_id
   - Reuse feed components

**Testing Focus:** Player page loads with data, JS interactions work correctly.

### Phase 4: Polish & Optimization

**Goal:** Responsive design, performance, edge cases.

**Tasks:**

1. **Responsive CSS**
   - Mobile breakpoints
   - Touch-friendly interactions

2. **Performance**
   - Database query optimization
   - Asset minification considerations

3. **Error Handling**
   - 404 pages
   - Empty state designs
   - Loading states

4. **Search Functionality**
   - Implement search endpoint
   - Autocomplete JS

**Testing Focus:** Mobile rendering, edge cases, error states.

---

## File Organization

### Target Structure

```
app/
├── routes/
│   ├── __init__.py
│   ├── ui.py              # Homepage, player detail, compare pages
│   ├── api.py             # JSON API endpoints
│   └── players.py         # Existing CRUD (keep for admin)
├── services/
│   ├── __init__.py
│   ├── player.py          # Player data aggregation
│   ├── metrics.py         # Percentile/metric queries
│   ├── similarity.py      # Comparison logic
│   ├── consensus.py       # Mock draft data
│   └── news.py            # News feed queries
├── schemas/
│   ├── ... (existing)
│   ├── consensus.py       # NEW: ConsensusRank
│   └── news.py            # NEW: NewsItem
├── models/
│   ├── ... (existing)
│   ├── responses.py       # NEW: API response models
├── templates/
│   ├── base.html
│   ├── home.html
│   ├── player_detail.html
│   ├── compare.html       # Optional: dedicated compare page
│   └── partials/
│       ├── _navbar.html
│       ├── _footer.html
│       ├── _ticker.html
│       ├── _mock_table.html
│       ├── _prospect_card.html
│       ├── _vs_arena.html
│       ├── _feed_item.html
│       ├── _player_bio.html
│       ├── _scoreboard.html
│       ├── _percentile_bars.html
│       └── _comparison_grid.html
└── static/
    ├── main.css           # Design tokens + global styles
    ├── main.js            # Global JS utilities
    ├── home.css           # Homepage-specific styles
    ├── home.js            # Homepage modules (ticker, arena)
    ├── player.css         # Player page styles
    └── player.js          # Player page modules (perf, comps)
```

---

## CSS Architecture

### Design Token Extraction

From mockups, extract to `main.css`:

```css
:root {
  /* Typography */
  --font-heading: 'Russo One', system-ui, sans-serif;
  --font-mono: 'Azeret Mono', ui-monospace, monospace;
  --font-body: system-ui, -apple-system, sans-serif;

  /* Primary Colors */
  --color-primary: #4A7FB8;        /* Blue navbar */
  --color-secondary: #E8B4A8;      /* Peach footer */

  /* Accent Colors */
  --color-accent-emerald: #10b981; /* Consensus, positive */
  --color-accent-rose: #f43f5e;    /* Negative changes */
  --color-accent-indigo: #6366f1;  /* Interactive, prospects */
  --color-accent-cyan: #06b6d4;    /* News, info */
  --color-accent-amber: #f59e0b;   /* Specials, warnings */
  --color-accent-fuchsia: #d946ef; /* VS Arena */

  /* Neutral Colors */
  --color-white: #ffffff;
  --color-slate-50: #f8fafc;
  --color-slate-100: #f1f5f9;
  --color-slate-200: #e2e8f0;
  --color-slate-300: #cbd5e1;
  --color-slate-500: #64748b;
  --color-slate-600: #475569;
  --color-slate-700: #334155;
  --color-slate-800: #1e293b;
  --color-slate-900: #0f172a;

  /* Layout */
  --max-width: 1280px;
  --content-width: 80%;
  --spacing-unit: 1rem;
  --border-radius: 0.75rem;
  --transition-speed: 0.2s;
}
```

### Component Classes

Following BEM-style naming per `CLAUDE.md`:

```css
/* Cards */
.card { }
.card__header { }
.card__content { }
.card--ring-emerald { }
.card--ring-indigo { }

/* Prospect Cards */
.prospect-card { }
.prospect-card__image { }
.prospect-card__info { }
.prospect-card__stats { }

/* Tables */
.data-table { }
.data-table__header { }
.data-table__row { }
.data-table__row--highlight { }

/* Badges */
.badge { }
.badge--riser { }
.badge--faller { }
.badge--similarity-high { }
```

---

## Summary

This roadmap provides a complete specification for implementing DraftGuru's frontend based on the mockups. Key principles:

1. **Phased approach** - Foundation → Homepage → Player Page → Polish
2. **Placeholder-first design** - Build with mock data, integrate real sources later
3. **Test-driven development** - Integration tests as primary validation
4. **Simple architecture** - Thin routes, service layer logic, Jinja templates
5. **Design consistency** - Follow style guide, use extracted design tokens

The implementation can proceed in parallel work streams once Phase 1 (Foundation) is complete, as Homepage and Player Page features are relatively independent.

---

*Document prepared by Claude Opus 4.5 on 2025-11-26*
