# Player Images Implementation

## Overview

Added player image support with **multiple style variants** per player, using **player ID-based naming** for deterministic script generation and a `?style=` query parameter for testing different visual styles across the app.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Naming convention** | `{player_id}_{style}.jpg` | Player ID is deterministic; scripts can query DB and generate images without knowing slugs |
| **Storage location** | `app/static/img/players/` | Served directly by FastAPI static mount |
| **Style selection** | `?style=` query param | Easy to flip between styles for A/B testing |
| **Default style** | `default` | Falls back gracefully when requested style missing |
| **Fallback** | `placehold.co` | Placeholder service when no local image exists |

## Available Styles

- `default` - Primary/production style
- `vector` - Vector art style
- `comic` - Comic book style
- `retro` - Retro/vintage style

Styles are defined in `app/utils/images.py:IMAGE_STYLES` and can be extended.

## File Structure

```
app/
├── static/
│   └── img/
│       └── players/
│           ├── .gitkeep
│           ├── 1_default.jpg    # Cooper Flagg - default
│           ├── 1_vector.jpg     # Cooper Flagg - vector
│           ├── 1_comic.jpg      # Cooper Flagg - comic
│           ├── 2_default.jpg    # Ace Bailey - default
│           └── ...
├── utils/
│   └── images.py                # Image URL helper functions
```

## Files Modified

| File | Change |
|------|--------|
| `app/static/img/players/.gitkeep` | Created directory |
| `app/utils/images.py` | **New** - Image URL helper with style support |
| `app/models/players.py` | Added `photo_url: Optional[str]` field to `PlayerProfileRead` |
| `app/services/player_service.py` | Sets `photo_url` using helper |
| `app/routes/ui.py` | Accepts `?style=` param on `/` and `/players/{slug}` |
| `app/templates/home.html` | Exposes `IMAGE_STYLE` and `PLAYER_ID_MAP` to JS |
| `app/templates/player-detail.html` | Exposes `IMAGE_STYLE` to JS |
| `app/static/js/home.js` | Added `ImageUtils` module for client-side image URL generation |
| `tests/unit/test_images.py` | **New** - Unit tests for image helper |
| `tests/integration/test_player_profile.py` | Added photo_url tests |

## Usage

### Adding Player Images (Manual)

```bash
# Find player ID from database
# SELECT id, display_name FROM players_master WHERE slug = 'cooper-flagg';
# Result: id = 1

# Copy images with correct naming
cp flagg-default.jpg app/static/img/players/1_default.jpg
cp flagg-vector.jpg app/static/img/players/1_vector.jpg
cp flagg-comic.jpg app/static/img/players/1_comic.jpg
```

### Testing Different Styles via URL

```
# Homepage - all player images use the style
/?style=vector
/?style=comic
/?style=retro

# Player detail page
/players/cooper-flagg?style=vector

# Without style param, uses 'default'
/players/cooper-flagg
```

### Programmatic Image Generation (Future)

Scripts can query the database and generate images deterministically:

```python
async def generate_images_for_all_players(db, prompt_template, style):
    result = await db.execute(
        select(PlayerMaster.id, PlayerMaster.display_name)
    )
    for row in result:
        image = generate_image(prompt_template.format(name=row.display_name))
        image.save(f"app/static/img/players/{row.id}_{style}.jpg")
```

## API

### `get_player_photo_url(player_id, display_name, style)`

Returns the appropriate image URL for a player.

**Parameters:**
- `player_id` (int): Player's database ID
- `display_name` (str, optional): Player's name for placeholder fallback
- `style` (str, optional): Image style (default, vector, comic, retro)

**Returns:** URL string - local static path if file exists, placeholder otherwise

**Fallback behavior:**
1. Check for `{player_id}_{style}.jpg`
2. If not found and style != default, check for `{player_id}_default.jpg`
3. If neither exists, return `placehold.co` placeholder URL

### `get_available_styles(player_id)`

Returns list of available styles for a player (useful for UI to show style picker).

## JavaScript API

The `ImageUtils` object is available in `home.js`:

```javascript
// Generate image URL with current style
ImageUtils.getPhotoUrl(playerId, displayName)

// Get player ID from slug (using server-provided map)
ImageUtils.getPlayerIdFromSlug(slug)
```

Global variables set by server:
- `window.IMAGE_STYLE` - Current style from `?style=` param (or null)
- `window.PLAYER_ID_MAP` - Object mapping slug -> player ID

## Notes

- No database migration required - photo URLs are computed at runtime
- Templates already referenced `{{ player.photo_url }}` - minimal template changes needed
- Images have `onerror` handlers in JS for graceful fallback when files don't exist
- The style system allows easy A/B testing of different visual approaches
