# Team & School Logo System

## Overview

Logos for NBA teams and college schools, stored in S3 alongside player images, with database metadata (colors, conference, division) to support visual branding across the site — player pages, mock draft boards, stats tables, and SVG share cards.

## Current State (NBA Complete, College Pending)

### What's Built

| Component | Status | Details |
|-----------|--------|---------|
| **`nba_teams` table** | Seeded (30 rows) | Name, abbreviation, slug, city, conference, division, brand colors |
| **`college_schools` table** | Created (empty) | Name, slug, conference, brand colors — ready for data |
| **NBA logos** | Collected (30/30) | 200×200 RGBA PNGs on S3 at `logos/nba/{slug}.png` |
| **College logos** | Not started | Blocked on school name deduplication |
| **Template integration** | Not started | Will implement after college logos are collected |
| **Share card integration** | Not started | Will embed logos as base64 data URIs |

### Schema

**`nba_teams`** (`app/schemas/nba_teams.py`)

| Column | Type | Example |
|--------|------|---------|
| `id` | int (PK) | 1 |
| `name` | str (indexed) | "Los Angeles Lakers" |
| `abbreviation` | str (unique) | "LAL" |
| `slug` | str (unique) | "lakers" |
| `city` | str | "Los Angeles" |
| `conference` | str | "Western" |
| `division` | str | "Pacific" |
| `logo_url` | str | `https://...s3.../logos/nba/lakers.png` |
| `primary_color` | str | "#552583" |
| `secondary_color` | str | "#FDB927" |

**`college_schools`** (`app/schemas/college_schools.py`)

| Column | Type | Example |
|--------|------|---------|
| `id` | int (PK) | 1 |
| `name` | str (unique) | "Duke" |
| `slug` | str (unique) | "duke" |
| `conference` | str (indexed) | "ACC" |
| `logo_url` | str | `https://...s3.../logos/college/duke.png` |
| `primary_color` | str | "#003087" |
| `secondary_color` | str | "#FFFFFF" |

### Scripts

| Script | Purpose | Usage |
|--------|---------|-------|
| `scripts/seed_nba_teams.py` | Seed all 30 NBA teams with metadata | `make nba-seed` |
| `scripts/collect_nba_logos.py` | Download, process (200×200 PNG), upload logos | `make nba-logos` |

**Logo collection flags:**
- `--dry-run` — download + process only, no upload or DB update
- `--local-only` — force local filesystem storage
- `--team ABBR` — process a single team (e.g., `--team LAL`)

### Utilities

**`get_logo_url(entity_type, slug)`** in `app/utils/images.py`

Builds a canonical logo URL using the same S3/CDN base as player images:
```
get_logo_url("nba", "lakers")
→ "https://.../logos/nba/lakers.png"

get_logo_url("college", "duke")
→ "https://.../logos/college/duke.png"
```

### S3 Layout

```
s3://draft-app-public-static/
├── players/                    # Existing player images
│   └── {id}_{slug}_{style}.png
└── logos/
    ├── nba/                    # ✅ 30 logos collected
    │   ├── lakers.png
    │   ├── celtics.png
    │   └── ...
    └── college/                # ⏳ Pending dedup + collection
        ├── duke.png
        └── ...
```

### Logo Processing Pipeline

1. **Download** — Fetch 500px PNG from ESPN CDN (`a.espncdn.com/i/teamlogos/nba/500/{abbr}.png`)
2. **Process** — Resize to 200×200 via Pillow, convert to RGBA, center on transparent canvas, optimize
3. **Upload** — Via `s3_client.upload()` (handles S3 in prod, local filesystem in dev)
4. **Link** — Update `logo_url` column in the corresponding DB row

ESPN uses non-standard slugs for some teams (mapped in `ESPN_ABBR_OVERRIDES`):
- Utah Jazz: `utah` (not `uta`)
- New Orleans Pelicans: `no` (not `nop`)

---

## Next Step: College School Deduplication

### The Problem

`players_master.school` contains **573 distinct string values**, but many are duplicates or non-college entries:

**Duplicate patterns:**
- Short vs full name: "Duke" / "Duke University"
- Nickname included: "Arizona" / "Arizona Wildcats"
- Official vs common: "BYU" / "Brigham Young University"
- Inconsistent format: "Miami (FL)" / "Miami (Florida)" / "Miami Hurricanes" / "University of Miami"

**Non-college entries (~20-30):**
- Professional clubs: "FC Barcelona Bàsquet", "KK Mega Basket", "Real Madrid (Spain)"
- G League: "Greensboro Swarm (NBA G League)", "Mexico City Capitanes (NBA G League)"
- International: "Guangzhou Loong Lions (CBA)", "New Zealand Breakers"
- Alternative paths: "Overtime Elite"

### Proposed Approach

1. **Build a canonical mapping** — Map all 573 raw values to canonical school names (or flag as non-college). AI-assisted draft, human-reviewed.
2. **Seed `college_schools`** — Insert the ~200-250 canonical schools with slug, conference, and colors.
3. **Collect college logos** — Same ESPN CDN pipeline, adapted for NCAA logos (requires ESPN team ID mapping since NCAA logos use IDs, not abbreviations).
4. **Optionally backfill** — Update `players_master.school` to use canonical names (or keep raw strings and join through a mapping table).

### Scope Estimate

- ~200-250 real US college programs after dedup
- ~20-30 non-college entries to flag/exclude
- ESPN has NCAA logos keyed by ESPN team ID (not by name), so we'll need a name → ESPN ID mapping

---

## Future: Template & Share Card Integration

Once both NBA and college logos are collected:

### Templates

Small inline logos alongside team/school text:
```html
<img class="team-logo team-logo--sm" src="{{ school_logo_url }}" alt="{{ player.college }}">
```

Display contexts:
- **Player detail page** — next to school name and draft team
- **Mock draft / consensus board** — beside each pick
- **Stats tables** — inline in the school column
- **News feed** — team badges on articles

### Share Cards (SVG Export)

Embed logos as base64 data URIs using the existing `image_embedder.py` pattern (same approach as player photos).

### Team Color Accents (Optional)

With `primary_color` / `secondary_color` stored per team, inject CSS custom properties:
```css
.player-card { --team-color: #552583; }
.player-card__accent { border-color: var(--team-color); }
```

Can also apply retro tinting via CSS filters on display rather than storing multiple logo variants:
```css
.team-logo--muted { filter: grayscale(0.6) sepia(0.2); }
```

---

## Alembic Migration History

| Revision | Description |
|----------|-------------|
| `b9705695210a` | Create `nba_teams` and `college_schools` tables (safe `IF NOT EXISTS` — tables may already exist via `AUTO_INIT_DB`) |
