# Admin Image Browsing Feature

This document describes the admin image browsing and management feature added to the DraftGuru admin panel.

## Overview

The feature provides administrators with the ability to:
1. Browse all generated player images in a grid gallery
2. Filter images by style, draft year, and player name
3. View image details including metadata and generation info
4. See player images directly on the player edit form
5. Delete images and queue regeneration
6. Detect "untracked" images that exist in S3 but aren't in the database

## Architecture

### Service Layer

**File:** `app/services/admin_image_service.py`

Provides query functions for image management:

```python
@dataclass
class ImageAssetInfo:
    """Image asset with snapshot context for admin display."""
    id: int
    player_id: int
    player_name: str
    player_slug: str
    style: str
    public_url: str
    generated_at: datetime
    is_current: bool
    snapshot_id: int
    snapshot_version: int
    file_size_bytes: int | None
    error_message: str | None
    used_likeness_ref: bool
    reference_image_url: str | None

@dataclass
class ImageListResult:
    """Paginated image list with filter metadata."""
    images: list[ImageAssetInfo]
    total: int
    styles: list[str]
    draft_years: list[int]
```

**Functions:**
- `list_images()` - List images with filters (style, player_id, draft_year, q, current_only, include_errors) and pagination
- `get_images_for_player()` - Get all images for a specific player
- `get_image_by_id()` - Get single image details
- `delete_image()` - Delete an image asset from the database

### Routes

**File:** `app/routes/admin/images.py`

| Route | Method | Description |
|-------|--------|-------------|
| `/admin/images` | GET | List all images with filters and pagination |
| `/admin/images/{asset_id}` | GET | View image detail page |
| `/admin/images/{asset_id}/delete` | POST | Delete an image |
| `/admin/images/{asset_id}/regenerate` | POST | Queue image for regeneration (placeholder) |

### Templates

| Template | Description |
|----------|-------------|
| `app/templates/admin/images/index.html` | Grid gallery view with filters |
| `app/templates/admin/images/detail.html` | Single image detail page |

### Player Detail Integration

The player edit form (`app/templates/admin/players/detail.html`) now includes:
- A right sidebar showing the player's generated images
- Two-column layout with form on left, images on right
- Sticky sidebar so images stay visible while scrolling
- Fallback detection for "untracked" S3 images

## Database Schema

The feature queries existing tables:

- `player_image_assets` - Individual image records with S3 URLs
- `player_image_snapshots` - Batch generation run metadata
- `players_master` - Player information

Key relationships:
```
PlayerImageAsset.snapshot_id -> PlayerImageSnapshot.id
PlayerImageAsset.player_id -> PlayerMaster.id
```

## Filtering Options

### Image List Filters
- **Player search (q)**: Case-insensitive search on player display name
- **Style**: Filter by image style (default, vector, comic, retro)
- **Draft Year**: Filter by player's draft year
- **Current Only**: Toggle to show only images from "current" snapshots
- **Include Errors**: Toggle to show images with generation errors

### Pagination
- Default: 48 images per page (6x8 grid)
- Maximum: 100 images per page

## Untracked Image Detection

When no database records exist for a player's images, the admin panel:

1. Constructs the expected S3 URL using the same pattern as the public site:
   ```
   {s3_base}/players/{player_id}_{slug}_{style}.png
   ```

2. Attempts to load the image directly from S3

3. If successful, displays with an "untracked" badge (dashed amber border)

4. If the image fails to load, shows "No image found in S3"

This helps identify data integrity issues where images exist in storage but aren't recorded in the database.

## CSS Classes

New CSS classes added to `app/static/css/admin.css`:

### Layout
- `.admin-player-detail-layout` - Two-column grid for player edit page
- `.admin-player-detail-layout__sidebar` - Sticky sidebar for images

### Image Grid
- `.admin-image-grid` - Responsive grid container
- `.admin-image-grid--small` - Smaller grid for inline displays
- `.admin-image-grid--sidebar` - Single-column grid for sidebar

### Image Cards
- `.admin-image-card` - Clickable image card with hover effects
- `.admin-image-card--untracked` - Dashed border for untracked images
- `.admin-image-card__overlay` - Gradient overlay for text
- `.admin-image-card__name` - Player name text
- `.admin-image-card__meta` - Badge container
- `.admin-image-card__error` - Error state display

### Image Detail
- `.admin-image-detail` - Two-column detail layout
- `.admin-image-detail__preview` - Image preview container
- `.admin-image-detail__meta` - Metadata sidebar
- `.admin-image-detail__actions` - Action buttons
- `.admin-image-detail__error` - Error message display

### Style Badges
- `.admin-badge--style-default` - Gray badge
- `.admin-badge--style-vector` - Cyan badge
- `.admin-badge--style-comic` - Amber badge
- `.admin-badge--style-retro` - Rose badge

### Utilities
- `.admin-meta-list` - Definition list for metadata
- `.admin-filters__field--wide` - Expanded filter field

## Navigation

Added "Images" link to admin sidebar navigation in `app/templates/admin/base.html`, visible to admin users under "Data Management" section.

## Image Regeneration with Preview/Accept Flow

Added in February 2026, this feature allows admins to regenerate player images with a preview step before committing.

### User Flow

1. Admin clicks "Regenerate" on image detail page (`/admin/images/{asset_id}`)
2. System generates new image via Gemini API (takes ~15-20s)
3. Admin sees preview page with side-by-side comparison:
   - Current image (left)
   - Generated preview (right)
   - Generation metadata (time, file size, likeness ref used)
4. Admin chooses one of three actions:
   - **Accept & Save**: Uploads to S3, updates asset record, redirects to detail with success
   - **Try Again**: Generates a new preview, replaces current preview
   - **Reject**: Discards preview, redirects back to original image detail

### Database Schema

**New Table: `pending_image_previews`**

Stores preview image data as base64 until admin accepts or rejects:

```sql
CREATE TABLE pending_image_previews (
    id SERIAL PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES players_master(id) ON DELETE CASCADE,
    source_asset_id INTEGER REFERENCES player_image_assets(id) ON DELETE SET NULL,
    style VARCHAR NOT NULL,
    image_data_base64 TEXT NOT NULL,
    file_size_bytes INTEGER NOT NULL,
    user_prompt TEXT NOT NULL,
    likeness_description TEXT,
    used_likeness_ref BOOLEAN DEFAULT FALSE,
    reference_image_url VARCHAR,
    generation_time_sec FLOAT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL  -- TTL for cleanup (24 hours)
);

CREATE INDEX ix_pending_previews_player ON pending_image_previews(player_id);
CREATE INDEX ix_pending_previews_expires ON pending_image_previews(expires_at);
```

**Migration:** `alembic/versions/e4f5g6h7i8j9_add_pending_image_previews_table.py`

### New Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/admin/images/{asset_id}/regenerate` | POST | Generate preview, redirect to preview page |
| `/admin/images/preview/{preview_id}` | GET | Show preview with Accept/Reject/Try Again |
| `/admin/images/preview/{preview_id}/accept` | POST | Upload to S3, update asset, delete preview |
| `/admin/images/preview/{preview_id}/reject` | POST | Delete preview, redirect back |
| `/admin/images/preview/{preview_id}/retry` | POST | Generate new preview, replace current |

### Service Layer Additions

**File:** `app/services/image_generation.py`

```python
@dataclass
class PreviewResult:
    """Result from generating a preview image (not yet uploaded to S3)."""
    image_data: bytes
    user_prompt: str
    likeness_description: str | None
    used_likeness_ref: bool
    reference_image_url: str | None
    generation_time_sec: float

async def generate_preview(
    self,
    player: PlayerMaster,
    style: str = "default",
    fetch_likeness: bool = False,
    ...
) -> PreviewResult:
    """Generate preview without uploading to S3."""
```

**File:** `app/services/admin_image_service.py`

```python
@dataclass
class PreviewInfo:
    """Preview image with context for admin display."""
    id: int
    player_id: int
    player_name: str
    # ... metadata fields
    image_data_base64: str  # For inline display
    current_image_url: str | None  # For side-by-side comparison

async def create_preview(db, player_id, source_asset_id, style, preview_result) -> PendingImagePreview
async def get_preview_by_id(db, preview_id) -> PreviewInfo | None
async def delete_preview(db, preview_id) -> bool
async def approve_preview(db, preview_id) -> PlayerImageAsset | None
```

### Template

**File:** `app/templates/admin/images/preview.html`

Side-by-side comparison layout with:
- Current image (if exists) on left
- Generated preview on right (displayed via base64 data URI)
- Generation metadata below
- Three action buttons: Accept & Save, Try Again, Reject

### Cache-Busting

To ensure browsers display regenerated images immediately, all image URLs include a cache-busting query parameter based on `generated_at` timestamp:

```
https://bucket.s3.amazonaws.com/players/123_player-slug_default.png?v=1706810198
```

This is added in:
- `approve_preview()` when storing `public_url`
- `ImageAssetInfo` construction for `display_url`

### Architecture Decision

Preview images are stored as **base64 in the database** rather than a temp S3 location:
- Simpler implementation (no temp S3 cleanup needed)
- Single-player regeneration = small data size (~100-500KB per image)
- Avoids orphaned temp files on server restart
- Preview records have TTL (24 hours) for cleanup of abandoned previews

## Future Enhancements

1. **Bulk Operations**: Add ability to select multiple images for bulk delete or regenerate.

2. **Image Comparison**: Side-by-side comparison of different styles or versions.

3. **S3 Sync Tool**: Tool to scan S3 and create database records for untracked images.

4. **Preview Cleanup Job**: Background job to delete expired previews from `pending_image_previews`.
