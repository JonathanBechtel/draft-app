# DraftGuru Implementation Roadmap (Codex)

Context: Build the FastAPI + Jinja + vanilla CSS/JS app to match the `mockups/` prototype while honoring `AGENTS.md`, `docs/style_guide.md`, and `docs/BUSINESS_MODELS.md`. Database tables that already exist are populated; new schema/integrations should be called out explicitly with placeholders.

## Architecture Plan
- **Stack**: FastAPI + SQLModel/async SQLAlchemy + Jinja templates; static assets under `app/static` (global `main.css`/`main.js` and per-page CSS/JS via `{% block extra_css/js %}`); templates under `app/templates`. No bundler.
- **Routing**: Thin route handlers in `app/routes/` using `AsyncSession` via `Depends(get_session)`; delegate logic to services in `app/services/`. Always set `response_model` and `status_code`.
- **Data layer**: Use existing SQLModel tables in `app/schemas/` (players, combine, metrics). Keep request/response models in `app/models/`. Any new tables require Alembic revisions.
- **Caching**: Add lightweight in-memory caching (e.g., `functools.lru_cache` for static lists, or short-lived dict cache) for heavy reads like consensus mocks and metrics. Wrap with invalidation hooks after ingestion jobs.
- **Templating**: Jinja templates with partials for nav, footer, ticker rows, prospect cards, VS Arena comparison row, percentile bars, news items. Respect style guide (Russo One headings, Azeret Mono data, soft neutrals + accent colors).
- **Frontend discipline**: Pure vanilla CSS + vanilla JS only (no component libraries/frameworks); reuse tokens from `main.css`, keep JS modular and page-scoped.
- **Assets**: Sprite/placeholder images hosted under `/static/img/placeholders/` until real assets arrive. Ensure `<img>` tags accept `src` from DB/CDN once available.
- **Share cards**: Reserve `/share` service with stub that returns JSON/SVG spec; actual PNG rendering pipeline to be added later (placeholder endpoint + unit test).

## Feature Breakdown & Implementation Notes

### Navigation + Search (global)
- **UI**: Fixed blue navbar with brand + search bar as in mockups.
- **Backend**: `/` home route renders template; search endpoint `GET /search?query=` returns JSON list of players (name, position, team, photo url placeholder). Service uses `players_master` + aliases. Add simple debounce in `main.js`.
- **Tests**: Unit: search service tokenizes and matches aliases; Integration: HTTPX call returns ordered, deterministic results and handles empty query.

### Home: Market Moves Ticker
- **Data**: Combine recent risers/fallers from mock history deltas + sentiment volume (existing metrics tables). If not available, expose placeholder with sample JSON seeded from DB top movers.
- **UI**: Ticker container populated via JS; loop animation.
- **Tests**: Unit: ticker data serializer clamps to N items and sorts by absolute change; Integration: `/api/market-moves` returns 200 + expected shape.

### Home: Consensus Mock Draft
- **Data**: Use precomputed consensus table (averaged pick, delta, odds if present). DB already populated; if odds absent, display `—`. Endpoint `/api/consensus` returning picks with ordering and team placeholders.
- **UI**: Table filled by JS; keep overflow scroll.
- **Tests**: Integration: ordering by pick, response model matches schema; snapshot test for first rows using HTTPX + test DB fixtures.

### Home: Top Prospects Grid
- **Data**: Top N prospects by consensus rank/score; include position, school, percentile highlights, placeholder photo URL.
- **UI**: Cards with hover effects per style guide; link to player pages `/players/{slug}`.
- **Tests**: Unit: service respects limit and sort; Integration: grid endpoint returns N unique players.

### Home: VS Arena (Player Comparison)
- **Data**: Endpoint `/api/compare?player_a=&player_b=&category=` returning merged metrics (anthro, combine performance, advanced stats) and similarity score. Use existing metric snapshots; if similarity not available, return placeholder and flag.
- **UI**: JS populates select options, badge, comparison table; images placeholders. Tabs switch category client-side.
- **Tests**: Unit: comparison service handles missing metrics gracefully; Integration: HTTPX call asserts merged payload, correct 404 on unknown player.

### Home: Live Feed / News
- **Data**: Needs RSS ingestion (new integration). Plan: create `news_items` table + ingestion job later; for now expose placeholder API that pulls from mock data file or simple scraper stub. Include tags (riser/faller).
- **UI**: Feed list with badges. CTA to player pages.
- **Tests**: Unit: feed serializer truncates title and maps tag colors; Integration: placeholder endpoint returns bounded list.

### Home: Draft Position Specials (Affiliate hooks)
- **Data**: CTA blocks referencing sportsbook offers. Use config-driven YAML/JSON under `app/config` to avoid hardcoding; display odds if present, else `TBD`.
- **Tests**: Unit: config loader filters by geo flag; Integration: template renders CTA when config present.

### Player Page: Hero (Bio + Scoreboard)
- **Data**: Player bio (name, school, class, age, hometown), measurements (height, weight, wingspan), consensus rank, buzz score, true position/range, trend. All pulled from existing player + metrics tables; if buzz/trend missing, show neutral placeholders.
- **UI**: Photo, bio, scoreboard panels with scanlines; follow mockup.
- **Tests**: Integration: `/players/{slug}` returns 200, contains key fields; Unit: service maps DB rows to view model with fallbacks.

### Player Page: Performance Percentiles
- **Data**: Precomputed percentiles by cohort/position across categories (anthro/combine/advanced). Endpoint `/api/players/{id}/percentiles?cohort=&position_only=`. If cohort not supported, 400 with allowed list.
- **UI**: Tabs + toggle; horizontal bars with labels and values.
- **Tests**: Unit: percentile query validates cohort and filters by position when flag set; Integration: HTTPX returns sorted metrics and percent values between 0–100.

### Player Page: Similarity Comps
- **Data**: KNN results from similarity tables (already in docs). Service returns top comps with score, year, archetype. Placeholder if empty.
- **UI**: Comps list with badges and win/lose coloring.
- **Tests**: Unit: comps service dedupes and caps at 5; Integration: endpoint returns expected shape.

### Player Page: Combine Results + Advanced Stats
- **Data**: Use combine tables (`combine_anthro`, `combine_agility`, `combine_shooting_results`) and advanced metrics snapshot. Provide canonical units and decimal formatting.
- **UI**: Tables with responsive overflow; highlight best values.
- **Tests**: Unit: formatting helper rounds correctly; Integration: tables render rows for available seasons.

### Player Page: Mock History & News
- **Data**: Mock history time series (avg pick over time). Provide JSON for sparkline (client-side simple SVG). News uses same feed as home, filtered by player id.
- **UI**: History card with mini chart; news list.
- **Tests**: Unit: history service orders by date; Integration: 200 with chronological points.

### Share & Social
- **Plan**: Provide `/share/player/{id}` and `/share/compare?...` endpoints returning JSON spec of SVG nodes (title, stats, photo url). Stub out PNG rendering (future Playwright/WeasyPrint). Add CTA buttons in templates calling share API; JS copies shareable link.
- **Tests**: Unit: share spec builder outputs branded colors; Integration: endpoint returns valid JSON schema.

## Launch Toggles & Feature Flags
- **Config-driven flags**: Add booleans in `app/config.py` sourced from env (default off) for business/optional surfaces: `AFFILIATE_OFFERS_ENABLED`, `NEWS_FEED_ENABLED`, `SHARE_CARDS_ENABLED`, `PROMO_TICKER_ENABLED`.
- **Template guards**: Wrap specials, promo ticker, share buttons, and news blocks in Jinja `if` checks; when off, omit section/dividers entirely to avoid gaps.
- **API guards**: Disabled features return 404 or empty payloads with `{"enabled": false}` hint; document in OpenAPI. Avoid hitting integrations when off.
- **Testing**: Unit: config defaults false and flip behavior; Integration: override settings dependency to confirm sections hide and endpoints gate correctly.
- **Admin toggle readiness**: Keep flags centralized so admin UI can flip them later; ensure idempotent cache invalidation when toggled.

## Integrations & Placeholders
- **RSS/News**: New ingestion module (`scrapers/rss.py`) with stub feed list; DB table `news_items` + Alembic migration to follow. Until then, seed with mock JSON file for local.
- **Affiliate Offers**: Configurable promotions file; link to sportsbook UTMs. Guard by feature flag.
- **Images**: Placeholder CDN path or `/static/img/placeholder-player.png`. Template accepts `photo_url` and falls back to placeholder.
- **Share Rendering**: Stub only; mark TODO for headless browser-based PNG export.

## Testing Strategy
- **Unit (tests/unit/)**: Services (search, percentiles, comparisons), formatting helpers, config loaders, share spec builder. Use factory fixtures for players/metrics without DB if possible.
- **Integration (tests/integration/)**: HTTPX client hitting FastAPI routes with test DB; cover home endpoints, player page, compare API, consensus API. Use `PYTEST_ALLOW_DB=1` + `TEST_DATABASE_URL` for async Postgres.
- **Snapshots**: Consider pytest-snapshot for API payload shapes (mock table rows, percentiles).
- **Accessibility/HTML smoke**: Use `BeautifulSoup` in integration tests to assert critical elements exist (nav, sections, tables).
- **Performance checks**: Simple timing assertion for consensus endpoint (<300ms on test data) to keep pages snappy.

## Delivery Phasing
1) **Scaffold pages & routing**: Base templates, nav/footer partials, global CSS/JS hooks, placeholder data hooks for all sections.
2) **Data wiring**: Connect consensus, top prospects, player bio/metrics to live DB; add comparison API.
3) **News & specials**: Add placeholder feed + config-driven affiliate CTAs.
4) **Share stubs & polish**: Share spec endpoints, hover/motion polish per style guide, responsive tweaks.
5) **Testing pass**: Run `make fmt`, `make lint`, `pytest tests/unit`, then `pytest tests/integration -q` with disposable DB.

## Assumptions & Open Questions
- The current demo app can be replaced; build fresh in a new worktree without preserving existing demo UI.
- DB already has consensus, metrics, and combine tables populated; if any missing, add migration + ingestion before wiring UI.
- Search and similarity tables exist; if similarity not present, we return empty state rather than blocking page.
- Odds/promotions require partner data; placeholders will show neutral CTA.
- Images arrive later; ensure templates gracefully degrade to placeholders without layout shift.
