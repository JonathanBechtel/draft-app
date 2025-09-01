# DraftGuru — V1_ROADMAP (post‑card version)

_Last updated: 2025‑09‑01_

## Purpose & scope
Deliver a lean, fast, and shareable NBA draft analytics site that showcases: Player Page, Player Compare, Consensus Mock, and a lightweight News/Feed — plus one‑click social sharing. Keep the stack simple and conventional to reduce AI‑assisted coding friction.

---

## Tech stack
**Backend:** Python · FastAPI · SQLModel · Alembic · Postgres (Neon)

**Frontend:** Jinja templates · Vanilla CSS · Vanilla JS

**Infra:** Fly.io (app hosting) · GitHub (repo) · GitHub Actions (CI/CD) · Neon (DB)

**Testing:** pytest (+ httpx for API)

> Notes: No pre‑commit hooks. No pgvector. Similarity done offline using scikit‑learn. Keep schema boring/portable.

---

## Product pillars (V1)
1. **Player Page (MVP):** Bio + stylized image, consensus slot, trend badge, percentile bars, top‑5 comps.
2. **Player vs Player Compare:** Head‑to‑head across Anthro · Combine · Production · Similarity; deep‑linkable.
3. **Consensus Mock (Aggregate):** Normalize multiple mocks → robust blended rank (Top‑30 + per‑player snippet).
4. **News/Feed (Real‑time‑ish):** Curated RSS ingestion for class feed + per‑player feed; deduped & timestamped.
5. **Share/Tweet everywhere:** One‑click export of Player, Compare, Consensus, and Feed cards with link‑back.
6. **Admin (minimal):** Login, data freshness, job runs, mock/feed source registry, run/refresh actions, image approvals.

---

## Data domains (category‑first, not tables)
1) **Identity & Canonicalization**  
   Canonical Player (stable id, name, position, draft class) · Source Player (raw ids/names per site) · Mapping decisions & audit trail.

2) **Attributes & Measurements**  
   Bio (height/weight/wingspan, school, birth info) · Combine / Anthropometrics · Production/boxscore aggregates.

3) **Rankings & Consensus**  
   Mock sources (name, url, weight, freshness) · Per‑source ranks · Consensus rank (normalized blend with outlier handling).

4) **Analytics & Caches**  
   Per‑metric z/percentile/rank (by cohort & optional position) · Nearest‑neighbors results (top‑k, per feature set) · Fast “card views.”

5) **Content & Assets**  
   News items (source, title, url, published_at, optional player link) · Image assets (hero/card, license, status) · Curated bios.

6) **Ops & Admin**  
   Source registry (RSS + mock providers) · Job runs/backfills · Auth (admin), audit logs.

---

## Phase plan (ship in vertical slices)

### 0) Skeleton & CI
- Health/version endpoints; Fly deploy from Actions.
- Alembic wired; Neon branches per env; seed script (10 demo players).
- CI runs `ruff/black --check` and `pytest` (no pre‑commit hooks).

### 1) UI Mockups (Jinja + vanilla CSS/JS)
- Static templates: Home, Player, Compare, Admin.  
- Reusable components (Card, Table, PercentBar, Badge) and a tiny utility CSS.

### 2) Core Data & Entity Resolution
- Minimal schema covering the six data domains.  
- Canonical ↔ Source mapping with deterministic rules + manual review path.  
- Read APIs: `/players`, `/players/{id}`, `/search`, `/compare`.

### 3) Historical Backfill & Analytics Compute (offline)
- scikit‑learn batches: Standardize features → z, percentiles, ranks per cohort.  
- NearestNeighbors (k ≈ 10–20) per feature set: Anthro, Combine, Production.  
- Persist caches/materialized views for fast page loads.

### 4) Player Page MVP
- Bio block, image, consensus snippet, trend arrow.  
- Percentile bars (≈8 key metrics) with cohort + position filters.  
- Top‑5 comps with similarity chips.  
- **Share:** export card to PNG + Tweet intent.

### 5) Player vs Player Compare
- Tabs: Anthro · Combine · Production · Similarity; diff badges + sparkbars; deep‑link URLs.  
- **Share:** export compare panel to PNG + Tweet intent.

### 6) Consensus Mock (Aggregate)
- Source registry + normalization + robust blend → consensus ranks.  
- Home Top‑30 and per‑player consensus snippet.  
- **Share:** export Top‑30 panel or single‑player consensus card.

### 7) News/Feed (Real‑time‑ish)
- Whitelisted RSS ingestion (cron), dedupe by `url+published_at`.  
- Class feed + per‑player feed.  
- **Share:** export a news card + link.

### 8) Admin & Auth (minimal)
- Admin login (session/JWT).  
- Panels: data freshness, job runs, mock sources, feed sources.  
- Actions: re‑run compute, upload new mock ranks, approve images.

### 9) Stylized Player Images (finish)
- Upload → approve → card/hero; fallback silhouette.  
- All key prospects have a card‑ready image.

---

## Share/Tweet system (lightweight spec)
- **Render as SVG**, then export: SVG → Canvas → `toDataURL()` → PNG download.  
- **Tweet intent** URL with prefilled text + canonical page link + `utm=share_button`.  
- **Embed brand footer** (site name/logo/url) in the SVG.  
- **Open Graph** tags per page; endpoint to generate a simple SVG→PNG `og:image` by player or compare pair.

---

## Operational guardrails (lean)
- CI gates: format/lint/test only (no pre‑commit hooks).  
- Migrations via Alembic; env‑scoped secrets; basic structured logging.  
- Nightly batch for analytics & consensus; RSS cron; simple caching headers on read endpoints.

---

## V1 success criteria
- Player page & Compare are fast; each player shows meaningful percentiles + 3–5 comps.  
- Consensus Top‑30 visible, timestamped, source list included.  
- Share buttons produce attractive PNGs + working tweet intents on Player, Compare, Consensus, Feed.  
- Admin can refresh data and approve assets without code changes.

---

## Out of scope (V1)
- Social network APIs beyond RSS.  
- Per‑user accounts (beyond admin).  
- Live boxscore sync; heavy visualization libs; complex fuzzy matching.

---

## Notes for future detail
Convert phases into a granular checklist (0.1, 0.2, …) only when you’re ready; keep the roadmap stable and lightweight until then.

