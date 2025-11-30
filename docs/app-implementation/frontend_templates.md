# Frontend Templates Implementation

**Date:** 2025-11-30
**Branch:** `feature/front-end-wire`
**Commit:** `8574c4e`

## Overview

Converted the static HTML mockups (`mockups/draftguru_homepage.html` and `mockups/draftguru_player.html`) into a proper Jinja2 template system with modular CSS and JavaScript.

## Files Created

### Templates (`app/templates/`)

| File | Description |
|------|-------------|
| `base.html` | Master template with navbar/footer includes, CSS/JS blocks |
| `home.html` | Homepage with mock draft, prospects, VS Arena, news feed |
| `player-detail.html` | Player profile with bio, scoreboard, metrics, comparisons |
| `partials/navbar.html` | Reusable navigation bar component |
| `partials/footer.html` | Reusable footer with link columns |
| `partials/icons.html` | SVG icon definitions (currently unused, icons inline) |

### Stylesheets (`app/static/`)

| File | Description |
|------|-------------|
| `main.css` | Shared styles: variables, reset, navbar, footer, cards, H2H, feed |
| `css/home.css` | Homepage-specific: ticker, tables, prospect cards, specials |
| `css/player-detail.css` | Player page: bio, scoreboard, performance bars, comparisons |

### JavaScript (`app/static/`)

| File | Description |
|------|-------------|
| `main.js` | Shared functionality (search) |
| `js/home.js` | Homepage modules: Ticker, MockTable, Prospects, H2H, Feed, Specials |
| `js/player-detail.js` | Player modules: Scoreboard, Performance, Comparisons, H2H, Feed |

### Routes (`app/routes/ui.py`)

- `GET /` - Homepage with placeholder data
- `GET /players/{slug}` - Player detail with slug-based routing

## Architecture Decisions

### CSS Organization
- **Shared styles in `main.css`**: Design tokens, layout utilities, navbar, footer, cards, H2H comparison, news feed
- **Page-specific styles**: Only styles unique to that page (ticker on home, scoreboard on player-detail)
- **BEM-style naming**: `.card`, `.card-ring`, `.h2h-player-photo`, etc.

### JavaScript Pattern
- **Module pattern**: Each feature is a self-contained object with `init()` method
- **Data injection**: Server passes data via `window.MOCK_PICKS`, `window.PLAYERS`, etc.
- **DOMContentLoaded**: All modules initialize when DOM is ready

### Routing
- **Slug-based URLs**: `/players/cooper-flagg` instead of `/players/1`
- **Tie-breaking**: For duplicate names, append numeric suffix (e.g., `john-smith-2`)

## Key Components

### Homepage Sections
1. **Market Moves Ticker** - Animated scrolling ticker showing risers/fallers
2. **Consensus Mock Draft** - Table with pick, player, position, team, avg rank, change
3. **Top Prospects** - Card grid with photos, stats pills, riser/faller badges
4. **VS Arena** - Head-to-head comparison with photos, category tabs, winner banner
5. **Live Draft Buzz** - News feed with source, title, time, tags
6. **Draft Position Specials** - Betting odds display

### Player Detail Sections
1. **Personal Info** - Photo, bio, secondary metadata
2. **Sports Scoreboard** - Consensus rank, buzz score, true position, wins added, trend
3. **Performance Metrics** - Percentile bars with category tabs (anthro, combine, stats)
4. **Head-to-Head Comparison** - Compare against other prospects
5. **Player Comparisons** - Similar players grid with similarity badges
6. **Player News Feed** - Player-specific news items

## Data Structure (Placeholder)

### Mock Picks
```python
{
    "pick": 1,
    "name": "Cooper Flagg",
    "slug": "cooper-flagg",
    "position": "F",
    "college": "Duke",
    "avgRank": 1.2,
    "change": 1
}
```

### Player (Extended)
```python
{
    "name": "Cooper Flagg",
    "slug": "cooper-flagg",
    "position": "Forward",
    "college": "Duke",
    "height": "6'9\"",
    "weight": "205 lbs",
    "age": 18,
    "class": "Freshman",
    "hometown": "Newport, ME",
    "wingspan": "7'2\"",
    "photo_url": "...",
    "metrics": {
        "consensusRank": 1,
        "consensusChange": 1,
        "buzzScore": 94,
        "truePosition": 1.0,
        "trueRange": 0.3,
        "winsAdded": 8.2,
        "trendDirection": "rising"
    }
}
```

## Design System Reference

- **Fonts**: Russo One (headings), Azeret Mono (data), system-ui (body)
- **Colors**: See `main.css` CSS variables (`:root`)
- **Accent colors**: emerald, rose, indigo, cyan, amber, fuchsia
- **Effects**: Scanlines overlay, dot-matrix backgrounds, pixel corner accents

## Next Steps

1. Connect templates to real backend data (database queries)
2. Implement search functionality
3. Add player comparison page
4. Wire up news feed to RSS ingestion
5. Add betting odds integration
