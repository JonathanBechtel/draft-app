# Shareable Image Export (SVG → PNG) — End-to-End Spec

  This spec defines DraftGuru share cards generated via a deterministic, browserless pipeline:
  **API data → SVG template → rasterize → PNG**.

  This is a “shadow rendering system”: it should feel *branded and consistent* with the site, optimized for social readability, and does **not** need to be pixel-perfect to the live HTML/CSS.

  ---

  ## 1) Goals & Non-Goals

  ### Goals
  - Generate **shareable PNG** cards for 4 analytics modules:
    - `vs_arena` (homepage)
    - `performance` (player detail)
    - `h2h` (player detail)
    - `comps` (player detail)
  - Typography quality is high priority: **Russo One** + **Azeret Mono**
  - Performance: low overhead per render, predictable resource usage, easy to scale
  - Determinism: stable pixel output for golden-image tests (with pinned fonts + renderer)

  ### Non-Goals
  - Pixel-perfect reproduction of on-page HTML/CSS
  - Browser screenshot rendering (Playwright/Chromium) as the primary approach
  - Auto-layout “magic”; we will cap lists and define fixed slots for stability

  ---

  ## 2) Output Requirements

  - **Format:** PNG
  - **Dimensions:** `1200×630` (2:1)
  - **Internal render size:** `2400×1260`, then downscale to `1200×630` for crisp text
  - **Always include:** footer watermark bar with `nbadraft.app`

  ---

  ## 3) Fonts (Hard Requirement)

  Do not rely on Google Fonts or any runtime web fetch.

  - Title font: **Russo One**
  - Data/labels font: **Azeret Mono** (tabular numerals preferred)
  - Bundle font files in-repo/container (e.g. `app/static/fonts/`)
  - Ensure rasterizer resolves these fonts deterministically.

  Two acceptable approaches:
  1) Configure renderer’s font discovery (preferred if supported cleanly in your stack)
  2) Embed fonts in the SVG (heavier SVGs, but fully self-contained)

  Text sizing targets (final 1200×630):
  - Title: 40–46px
  - Context line: 18–22px
  - Labels: 16–18px uppercase
  - Values: 22–28px tabular numerals

  ---

  ## 4) Design Tokens (Site-Compatible, Share-Optimized)

  We keep the established palette but optimize for shareability:
  - Higher contrast neutrals (white/slate-50 backgrounds; slate-900 text)
  - Accents used for identity, bars, badges, winner highlights
  - Patterns (scanlines/dot-matrix) are subtle (low opacity) to avoid muddy compression

  Suggested palette (aligned to mockups):
  - Neutrals:
    - slate-900 `#0f172a`
    - slate-800 `#1e293b`
    - slate-600 `#475569`
    - slate-200 `#e2e8f0`
    - slate-100 `#f1f5f9`
    - white `#ffffff`
  - Accents:
    - emerald `#10b981`
    - cyan `#06b6d4`
    - fuchsia `#d946ef`
    - indigo `#6366f1`
    - amber `#f59e0b`
    - rose `#f43f5e`

  Layout constants:
  - Outer safe padding: 48px
  - Header height: 120px (fixed)
  - Footer watermark bar height: 56px (fixed)

  ---

  ## 5) Determinism Rules (Critical for Testing)

  - No animations, transitions, time-dependent elements.
  - Fixed-height layout regions; no “content grows forever”.
  - Lists are capped:
    - VS rows: N=6
    - Performance rows: N=8
    - H2H rows: N=8
    - Comps tiles: K=6 (2×3)
  - Text rules:
    - Names: single-line, ellipsis
    - Bio/context: single-line, ellipsis
    - Metric labels: single-line; abbreviate at source if needed
  - Missing data:
    - Missing image: branded placeholder silhouette (local asset)
    - Missing values: render “—” and gray out row

  ---

  ## 6) Architecture & Rendering Approach

  ### Why SVG → PNG
  - Faster and lighter than Chromium
  - Deterministic outputs suitable for golden-image tests
  - Typography is controllable if fonts are bundled and pinned
  - Removes “page readiness” issues (no client JS, no async layout races)

  ### Renderer
  Recommendation: **resvg** (fast, deterministic SVG rasterization)
  - Keep SVG features simple (avoid expensive filters unless necessary)
  - Prefer strokes/gradients/pattern fills over blur/backdrop effects

  ### High-Level Flow
  1) Validate request
  2) Load data (DB/services)
  3) Build a **Render Model** (normalized, capped, formatted strings)
  4) Render SVG template with model
  5) Rasterize SVG → PNG at 2400×1260
  6) Downscale to 1200×630
  7) Store (S3 in prod, local in dev)
  8) Return URL + metadata

  ---

  ## 7) File/Module Structure (Proposed)

  - `app/routes/export.py` (POST endpoint)
  - `app/services/share_cards/`
    - `render_models.py` (builders per component)
    - `svg_templates.py` (template rendering wrapper)
    - `rasterizer.py` (SVG→PNG adapter)
    - `storage.py` (S3/local)
    - `cache_keys.py` (content-addressed keys + versioning)
  - `app/templates/export_svg/`
    - `_defs.svg` (gradients/patterns/badges)
    - `vs_arena.svg`
    - `performance.svg`
    - `h2h.svg`
    - `comps.svg`
  - `app/static/fonts/` (bundled font files)
  - `app/static/img/export/` (placeholder silhouette, brand marks)
  - Tests:
    - `tests/unit/test_share_cards_*`
    - `tests/integration/test_export_api.py`
    - optional: `tests/golden/*.png` + visual test runner

  ---

  ## 8) API Contract

  ### Endpoint
  `POST /api/export/image`

  ### Request JSON
  ```json
  {
    "component": "performance",
    "player_ids": [1661],
    "context": {
      "comparison_group": "current_draft",
      "same_position": true,
      "metric_group": "combine"
    }
  }

  Enums:

  - component: vs_arena | performance | h2h | comps
  - comparison_group: current_draft | current_nba | all_time_draft | all_time_nba
  - metric_group: anthropometrics | combine | shooting | advanced

  Validation rules:

  - vs_arena, h2h require exactly 2 player_ids
  - performance, comps require exactly 1 player_ids
  - metric_group required for vs_arena, performance, h2h (can be optional for comps if comps don’t depend on it)
  - Reject unknown enums with 400
  - Missing player IDs in DB → 404

  ### Response JSON

  {
    "url": "https://.../exports/performance/1661_ab12cd34ef56.png",
    "title": "Cooper Flagg — Performance",
    "filename": "cooper-flagg-performance.png",
    "cached": true
  }

  Optional dev-only:

  - debug_svg_url (helpful for template iteration)

  ———

  ## 9) Render Models (Normalized, Capped)

  Render models are intentionally “presentation-ready”: already formatted strings and fixed-length lists.

  ### Common types

  - PlayerBadge:
      - name (string)
      - subtitle (string; e.g. “F • Duke (2025)”)
      - photo_data_uri (string; base64 data URI) OR photo_key if rasterizer can read local files
  - ContextLine:
      - comparison_group_label (string)
      - position_filter_label (string)
      - metric_group_label (string)
      - rendered (string; e.g. “Current Draft Class · Same Position · Combine”)

  ### vs_arena render model

  - title: “A vs B”
  - context_line
  - player_a: PlayerBadge
  - player_b: PlayerBadge
  - rows (length 6):
      - label
      - a_value
      - b_value
      - winner: a | b | tie | none
      - higher_is_better: bool (used to compute winner)
  - accent: fuchsia (module identity)

  ### performance render model

  - title: “Player — Performance”
  - context_line
  - player: PlayerBadge
  - rows (length 8):
      - label
      - value
      - percentile (0–100 int)
      - percentile_label (e.g. “92nd %ile”)
      - tier: elite|good|average|below|unknown
  - accent: emerald

  ### h2h render model

  - title
  - context_line
  - player_a: PlayerBadge
  - player_b: PlayerBadge
  - similarity_badge (optional; string like “92% Match”)
  - rows (length 8): same shape as VS
  - accent: fuchsia

  ### comps render model

  - title: “Player — Comparisons”
  - context_line
  - player: PlayerBadge
  - tiles (length 6):
      - name
      - subtitle (pos/school/year)
      - similarity (0–100)
      - similarity_label (e.g. “87%”)
      - photo_data_uri
      - tier (for badge color)
  - accent: cyan

  Deterministic selection:

  - Performance rows: use canonical “top 8” metric list per metric_group (define explicitly in code/config).
  - VS/H2H rows: canonical “top 6/8” list per metric_group, with metric direction specified.

  ———

  ## 10) Image Handling (Deterministic)

  Avoid network fetch inside rasterization:

  - Server fetches player images from existing storage (local/S3), resizes/crops deterministically, embeds as data URIs.

  Crop rules:

  - Center-crop with slight top bias (8–12%) to favor faces
  - Rounded-rect clip path
  - Add subtle border stroke for legibility on light backgrounds

  Fallback:

  - Local placeholder silhouette image (consistent across all cards)

  ———

  ## 11) Caching & Versioning

  Use content-addressed caching rather than time-based TTL for correctness.

  Inputs to cache key:

  - component
  - template_version (manual bump when templates/styles change)
  - normalized player_ids (sort if you want symmetry for vs_arena/h2h)
  - normalized context (sorted keys)
  - optionally: data fingerprint (updated_at timestamps / metric snapshot version)

  Key example:

  - exports/{component}/{sha256(normalized_inputs)[:16]}.png

  TTL may still be applied at CDN level, but cache correctness should come from versioned keys.

  ———

  ## 12) Pre-generation (Optional, Recommended)

  Nightly warm-cache job:

  - Top 30 prospects:
      - performance cards for each metric_group
      - comps cards
  - H2H:
      - popularity-based or 10×10 top prospects

  Pre-gen uses the same API/service path and writes to the same cache keys.

  ———

  ## 13) UI Integration

  ### Buttons

  Add a “Save as image” icon button in the top-right of each module:

  - VS Arena (homepage)
  - Performance (player detail)
  - H2H (player detail)
  - Comps (player detail)

  ### Modal flow

  1. Click → gather current context from UI state
  2. POST /api/export/image
  3. Modal shows:
      - loading state
      - preview <img src=url>
      - download button using filename
      - optional copy link
  4. Error state with retry

  Client-side optimization:

  - Cache the response in-session keyed by (component, player_ids, context) to avoid repeat requests.

  ———

  ## 14) Testing Strategy

  ### Unit (fast, no DB)

  - Cache key normalization determinism
  - Title/context formatting
  - Render model deterministic selection + list capping
  - Winner calculation direction (higher/lower is better)
  - Percentile tier mapping

  ### Integration (FastAPI + DB; rasterizer mocked)

  - Happy path returns {url,title,filename,cached}
  - Invalid component/context → 400
  - Missing player → 404
  - Cache hit bypasses rasterizer

  ### Visual regression (optional, separate job/marker)

  - Render fixed fixtures to PNG and compare to goldens
  - Requires pinned:
      - rasterizer version
      - font files
      - runtime environment (ideally container)

  ———

  ## 15) Observability & Ops

  Log per request:

  - component, cache_hit
  - durations: model build / svg render / rasterize / store
  - output size bytes
  - error category

  Resource safety:

  - per-IP rate limiting
  - concurrency limits on rasterization (even though it’s lighter than Chromium)

  ———

  ## 16) Implementation Checklist (Handoff)

  - [ ] Bundle Russo One + Azeret Mono fonts; document rasterizer font config
  - [ ] Define canonical metric lists + direction rules per metric_group
  - [ ] Implement render model builders (vs/perf/h2h/comps)
  - [ ] Author SVG templates + shared defs file
  - [ ] Implement SVG→PNG rasterizer adapter (resvg)
  - [ ] Implement content-addressed caching + S3/local storage
  - [ ] Implement POST /api/export/image response shape
  - [ ] Implement UI modal + download flow
  - [ ] Add unit + integration tests; optional visual golden suite
