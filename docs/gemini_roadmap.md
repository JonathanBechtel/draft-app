# Gemini Roadmap: DraftGuru V1 Implementation

**Date:** 2025-11-26  
**Model:** Gemini  
**Objective:** Transform the `mockups/` prototypes into a functional FastAPI application, adhering to the `DraftGuru` design philosophy and tech stack.

---

## 1. Architectural Overview

The application will follow the existing patterns defined in `GEMINI.md`:
*   **Backend:** FastAPI with SQLModel (Async SQLAlchemy) and Postgres.
*   **Frontend:** Jinja2 Templates (server-side rendering) + Vanilla JavaScript/CSS (no bundler).
*   **Data:** Offline pre-calculation for heavy analytics; fast reads for the UI.

### 1.1 Frontend Strategy
*   **Design Philosophy:** Strict adherence to "Light Retro" aesthetic using Vanilla CSS/JS. No frameworks (React, Vue, Tailwind, Bootstrap).
*   **Maintenance Level:** "Throwaway Prototype" — prioritize speed and visual fidelity over complex architecture. Code can be messy if it works and looks good.
*   **Base Template:** Refactor `mockups/draftguru_homepage.html` to extract the common layout (Navbar, Footer, CSS variables) into `app/templates/base.html`.
*   **Page Templates:** 
    *   `app/templates/index.html` (Homepage).
    *   `app/templates/player_detail.html` (Player Profile).
    *   `app/templates/compare.html` (VS Arena).
*   **Assets:**
    *   **CSS:** Use a single monolithic `app/static/main.css` file derived from the mockups. Avoid over-engineering component stylesheets.
    *   **JS:** Use a single `app/static/main.js` for global behavior. Page-specific logic (like the VS Arena calculator) can remain in `<script>` tags within the template or a simple sidecar file if it grows too large.

### 1.2 Backend Strategy
*   **Routes:**
    *   `GET /`: Renders Homepage.
    *   `GET /players/{slug}`: Renders Player Profile.
    *   `GET /compare`: Renders VS Arena.
    *   `GET /api/v1/ticker`: JSON endpoint for market moves (if dynamic updating is needed).
    *   `GET /api/v1/mock-draft`: JSON endpoint for consensus data.
*   **Services:**
    *   `ConsensusService`: Aggregates and returns mock draft rankings.
    *   `TickerService`: Calculates risers/fallers based on historical rank changes.
    *   `PlayerService`: (Existing) extend to support full profile data including "Buzz Score" and "Wins Added".
    *   `ComparisonService`: (New) Handles logic for "VS Arena" similarity and head-to-head metrics.

---

## 2. Gap Analysis & Data Modeling

The current schema (`app/schemas/`) lacks support for several key features shown in the mockups.

### 2.1 Consensus Mock Draft
**Missing:** No table to store aggregated mock draft rankings.
**Proposed Model:** `ConsensusMock`
*   `player_id`: FK to `PlayerMaster`.
*   `rank`: Integer (Current Consensus Rank).
*   `prev_rank`: Integer (For change calculation).
*   `team_id`: FK (or string for now) for the projected team.
*   `avg_rank`: Float (Average across sources).
*   `volatility`: Float (Standard deviation of ranking).

### 2.2 Market Moves / Ticker
**Missing:** No historical tracking of rank/stock to generate "Risers & Fallers".
**Proposed Model:** `PlayerStock` (or use `Snapshot` pattern)
*   `player_id`: FK.
*   `date`: Date.
*   `rank`: Integer.
*   `buzz_score`: Integer (0-100 calculated index).
*   **Logic:** Ticker displays top 5 positive and negative deltas over the last 7 days.

### 2.3 News Feed (Draft Buzz)
**Missing:** No storage for news items.
**Proposed Model:** `NewsItem`
*   `id`: PK.
*   `player_id`: FK.
*   `source`: String (e.g., "ESPN", "The Ringer").
*   `title`: String.
*   `url`: String.
*   `published_at`: Datetime.
*   `sentiment`: Enum (Riser/Faller/Neutral) - *Optional ML enhancement later.*

### 2.4 Player Extensions
**Existing:** `PlayerMaster`, `CombineAnthro`, `CombineAgility`.
**Needs:**
*   `PlayerBio`: Add fields for `hometown`, `class_year`, `age`.
*   `PlayerMetrics`: Add `wins_added` (placeholder), `buzz_score` (placeholder).

---

## 3. Integration Plan

### 3.1 Data Ingestion (Placeholders)
Since real data sources (scrapers) might not be fully ready for *new* features:
1.  **Seed Scripts:** Create `app/scripts/seed_mock_data.py` to populate `ConsensusMock` and `NewsItem` with plausible fake data for development.
2.  **Image Placeholders:** Use `https://placehold.co` (as in mockups) until the image pipeline is built.
3.  **Metric Backfill:** Script to calculate/fake `percentiles` and `similarity_scores` if offline pipeline isn't running yet.

### 3.2 Testing Strategy
*   **Integration Tests (`tests/integration/`)**:
    *   `test_routes_pages.py`: Verify HTML responses for `/`, `/players/{slug}`, etc.
    *   `test_api_consensus.py`: Verify JSON structure for mock draft data.
*   **Unit Tests (`tests/unit/`)**:
    *   `test_service_comparison.py`: Verify the logic for "Winner" determination in VS Arena.
    *   `test_service_ticker.py`: Verify sorting logic for risers/fallers.

---

## 4. Implementation Steps

### Step 1: Database Schema Expansion
*   Create `app/schemas/consensus.py` (`ConsensusMock`).
*   Create `app/schemas/content.py` (`NewsItem`).
*   Create `app/schemas/analytics.py` (`PlayerStock`).
*   Run `alembic revision --autogenerate` and apply.

### Step 2: Seed Data
*   Write `app/scripts/seed_roadmap_data.py` to populate these new tables with 10-20 sample records matching the mockup players (Cooper Flagg, Ace Bailey, etc.).

### Step 3: Backend Services
*   Implement `ConsensusService.get_current_rankings()`.
*   Implement `TickerService.get_market_moves()`.
*   Implement `ComparisonService.compare_players(id_a, id_b)`.

### Step 4: Frontend Base & Homepage
*   Create `app/templates/base.html` from `mockups/draftguru_homepage.html`.
*   Migrate CSS to `app/static/main.css`.
*   Implement `app/routes/ui.py::home` to inject `ticker_data`, `mock_data`, and `prospects` into the template.
*   Replace JS mock data in `index.html` with Jinja2 loop rendering.

### Step 5: Player Profile Page
*   Migrate `mockups/draftguru_player.html` to `app/templates/player_detail.html`.
*   Wire up `app/routes/ui.py::player_detail` to fetch `PlayerMaster` + `Combine` + `Consensus` data.

### Step 6: VS Arena (Interactive)
*   Decide on interaction model: 
    *   **Option A (Simpler):** pure Client-side JS using a JSON data attribute embedded in the page (good for small datasets < 100 players).
    *   **Option B (Robust):** HTMX or Fetch calls to `/api/compare?p1=X&p2=Y`.
    *   *Decision:* Start with Option A for the prototype to keep it "low-JS" and fast, as requested in `GEMINI.md`.

### Step 7: Verification
*   Run `pytest` suite.
*   Manual visual check against `docs/style_guide.md`.
