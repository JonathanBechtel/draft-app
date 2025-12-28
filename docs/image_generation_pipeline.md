# AI Image Generation Pipeline

## Overview

Automated player portrait generation using **Google Gemini's image generation API** (Nano Banana Pro), with **S3 storage**, **database auditing**, and **batch processing** by cohort or draft year.

This extends the original [player_images_implementation.md](./player_images_implementation.md) with AI-powered generation capabilities.

## Scope

### What Was Built

1. **CLI Script** (`scripts/generate_player_images.py`) - Batch generation tool
2. **Image Generation Service** (`app/services/image_generation.py`) - Reusable API integration
3. **S3 Storage** (`app/services/s3_client.py`) - Cloud storage with local fallback
4. **Audit Tables** - Full generation history tracking (like `metric_snapshots`)
5. **Likeness Enhancement** - Reference image description for better accuracy

### Key Features

| Feature | Description |
|---------|-------------|
| **Batch Generation** | Generate images for entire cohorts (draft class, NBA players) |
| **Cohort Filtering** | `--cohort current_draft`, `--draft-year 2025`, etc. |
| **Missing-Only Mode** | Skip players who already have images |
| **Reference Images** | Fetch player photos, describe features, improve likeness |
| **Cost Controls** | Configurable image size (512, 1K, 2K) |
| **Audit Trail** | Full prompt storage for admin review/regeneration |
| **S3 Storage** | Persistent cloud storage, CDN-ready |

---

## Technical Requirements

### Dependencies

```yaml
# Add to environment.yml
- pip:
  - google-genai    # Gemini API client
  - boto3           # AWS S3 SDK
```

### Environment Variables

```bash
# .env additions
GEMINI_API_KEY=your-api-key

# S3 Storage (production)
S3_BUCKET_NAME=draftguru-images
S3_REGION=us-east-1
S3_ACCESS_KEY_ID=your-access-key
S3_SECRET_ACCESS_KEY=your-secret-key
S3_PUBLIC_URL_BASE=https://draftguru-images.s3.amazonaws.com

# Optional: S3-compatible services (Tigris, R2, MinIO)
# S3_ENDPOINT_URL=https://fly.storage.tigris.dev

# Dev mode: use local filesystem
IMAGE_STORAGE_LOCAL=true
```

### Database Migrations

Two migrations were created:

```bash
# 1. Image snapshot tables
alembic/versions/a1b2c3d4e5f6_add_image_snapshot_tables.py

# 2. Reference image URL on PlayerMaster
alembic/versions/b2c3d4e5f6a7_add_reference_image_url_to_players.py

# Run migrations
alembic upgrade head
```

---

## Architecture

### File Structure

```
app/
├── config.py                          # New: Gemini + S3 settings
├── schemas/
│   ├── image_snapshots.py             # NEW: PlayerImageSnapshot, PlayerImageAsset
│   └── players_master.py              # Modified: +reference_image_url
├── services/
│   ├── s3_client.py                   # NEW: S3 upload/delete operations
│   └── image_generation.py            # NEW: Gemini API integration
├── utils/
│   └── images.py                      # Modified: new naming convention
scripts/
└── generate_player_images.py          # NEW: CLI batch generation
```

### Database Schema

```
┌─────────────────────────────┐
│   player_image_snapshots    │  (Audit trail for batch runs)
├─────────────────────────────┤
│ id                          │
│ run_key                     │  e.g., "draft_2025_v1"
│ version                     │  Monotonic within run_key
│ is_current                  │  Active snapshot marker
│ style                       │  "default", "vector", etc.
│ cohort                      │  current_draft, current_nba, etc.
│ draft_year                  │  Optional: for draft-specific batches
│ population_size             │
│ success_count / failure_count│
│ system_prompt               │  Full prompt text (for admin UI)
│ system_prompt_version       │  e.g., "v2.1"
│ image_size                  │  "512", "1K", "2K"
│ estimated_cost_usd          │
└─────────────────────────────┘
              │
              │ 1:N
              ▼
┌─────────────────────────────┐
│    player_image_assets      │  (Individual image records)
├─────────────────────────────┤
│ id                          │
│ snapshot_id (FK)            │
│ player_id (FK)              │
│ s3_key                      │  "players/123_cooper-flagg_default.png"
│ s3_bucket                   │
│ public_url                  │  CDN URL for serving
│ user_prompt                 │  Player-specific prompt (for admin UI)
│ likeness_description        │  From reference image analysis
│ used_likeness_ref           │  Boolean
│ reference_image_url         │  Source URL if used
│ generation_time_sec         │
│ error_message               │  If generation failed
└─────────────────────────────┘
```

### Naming Convention

**New format** (preferred):
```
{player_id}_{slug}_{style}.png
Example: 1661_cooper-flagg_default.png
```

**Legacy format** (backwards compatible):
```
{player_id}_{style}.jpg
Example: 1661_default.jpg
```

The image utility checks for new format first, falls back to legacy.

---

## Product Considerations

### Why S3 Storage?

| Approach | Pros | Cons |
|----------|------|------|
| **Local filesystem** | Simple | Lost on deploy, no scaling |
| **Fly Volumes** | Persists | Single-machine only |
| **S3/CDN** | Scales, CDN-ready, survives deploys | Small cost, more setup |

**Decision**: S3 for production, with `IMAGE_STORAGE_LOCAL=true` for dev.

### Cost Management

| Image Size | Est. Cost/Image | Use Case |
|------------|-----------------|----------|
| `512` | ~$0.02 | Testing, drafts |
| `1K` | ~$0.04 | Production (default) |
| `2K` | ~$0.08 | High-quality marketing |

CLI flags: `--size 512`, `--size 1K`, `--size 2K`

### Likeness Accuracy

For lesser-known players without strong internet representation:
1. Store a `reference_image_url` in the player's DB record
2. Use `--fetch-likeness` flag to fetch and describe the reference
3. Gemini vision generates a detailed description of facial features
4. This description is included in the generation prompt

This avoids copyright issues (no copyrighted image in the generation) while improving likeness.

### Future Admin UI Support

All prompts are stored in the database:
- `PlayerImageSnapshot.system_prompt` - The style/format instructions
- `PlayerImageAsset.user_prompt` - Player-specific generation prompt
- `PlayerImageAsset.likeness_description` - Reference image analysis

This enables a future admin panel where users can:
1. View a player's image generation history
2. See exactly what prompts were used
3. Tweak prompts and regenerate
4. A/B test different prompt versions

---

## Usage

### CLI Examples

```bash
# Generate for 2025 draft class
python scripts/generate_player_images.py --draft-year 2025 --run-key "draft_2025_v1"

# Generate for current NBA players, only those missing images
python scripts/generate_player_images.py --cohort current_nba --missing-only

# Generate for specific player with reference image for likeness
python scripts/generate_player_images.py --player-id 1661 --fetch-likeness

# Use explicit reference URL for a player
python scripts/generate_player_images.py --player-id 1661 \
  --likeness-url "https://example.com/player-photo.jpg"

# Cost-conscious batch: smaller size
python scripts/generate_player_images.py --cohort current_draft --size 512

# Dry run to preview and estimate costs
python scripts/generate_player_images.py --all --dry-run

# Limit to 5 players for testing
python scripts/generate_player_images.py --draft-year 2025 --limit 5
```

### CLI Arguments

| Argument | Description |
|----------|-------------|
| `--player-id` | Generate for specific player by ID |
| `--player-slug` | Generate for specific player by slug |
| `--cohort` | Filter by cohort: `current_draft`, `current_nba`, etc. |
| `--draft-year` | Filter by draft year (e.g., 2025) |
| `--all` | Generate for all players |
| `--style` | Image style: `default`, `vector`, `comic`, `retro` |
| `--missing-only` | Only generate if image doesn't exist |
| `--fetch-likeness` | Enable reference image description |
| `--likeness-url` | Explicit reference image URL |
| `--size` | Image size: `512`, `1K`, `2K` |
| `--run-key` | Unique run identifier |
| `--dry-run` | Preview without generating |
| `--limit` | Max players to process |
| `--notes` | Notes for this run |

### Programmatic Usage

```python
from app.services.image_generation import image_generation_service

# In an async context (e.g., admin route)
asset = await image_generation_service.generate_for_player(
    db=session,
    player=player,
    snapshot=snapshot,
    style="default",
    fetch_likeness=True,
    image_size="1K",
)
```

---

## Files Modified/Created

### New Files

| File | Purpose |
|------|---------|
| `app/schemas/image_snapshots.py` | `PlayerImageSnapshot` + `PlayerImageAsset` tables |
| `app/services/image_assets_service.py` | Resolve current image URLs from DB (serving) |
| `app/services/s3_client.py` | S3 upload/delete with local fallback |
| `app/services/image_generation.py` | `ImageGenerationService` class |
| `alembic/versions/a1b2c3d4e5f6_*.py` | Migration: snapshot tables |
| `alembic/versions/b2c3d4e5f6a7_*.py` | Migration: reference_image_url |
| `scripts/generate_player_images.py` | CLI batch generation script |

### Modified Files

| File | Changes |
|------|---------|
| `app/config.py` | Added Gemini API key, S3 settings, cost controls |
| `app/schemas/players_master.py` | Added `reference_image_url` field |
| `app/utils/images.py` | New `{id}_{slug}_{style}.png` format with legacy fallback |
| `app/services/player_service.py` | Resolve `photo_url` from current image assets |
| `app/routes/ui.py` | Serve `photo_url` from DB-backed image assets (S3) |
| `tests/unit/test_images.py` | Updated for new function signatures |

---

## Tests

### Unit Tests (`tests/unit/test_images.py`)

| Test | Description |
|------|-------------|
| `test_returns_placeholder_when_no_image_exists` | Fallback to placehold.co |
| `test_placeholder_uses_display_name` | Name in placeholder URL |
| `test_returns_new_format_png_when_exists` | New `{id}_{slug}_{style}.png` format |
| `test_returns_legacy_format_jpg_when_no_new_format` | Backwards compatibility |
| `test_prefers_new_format_over_legacy` | New format takes priority |
| `test_returns_requested_style_when_exists` | Style parameter works |
| `test_falls_back_to_default_when_requested_style_missing` | Default fallback |
| `test_falls_back_to_legacy_default_when_style_missing` | Legacy fallback chain |
| `test_returns_available_styles_new_format` | Detect new format styles |
| `test_returns_available_styles_legacy_format` | Detect legacy styles |
| `test_returns_mixed_format_styles` | Mixed format detection |

### Validation Results

```bash
make precommit                          # ✅ Passed
mypy app --ignore-missing-imports       # ✅ No errors (49 files)
pytest tests/unit -q                    # ✅ 21 passed
pytest tests/integration -q             # ✅ 39 passed
```

---

## System Prompt

The default system prompt is stored in `app/services/image_generation.py` and defines:

- **Output**: 800x1000 PNG, 4:5 aspect ratio
- **Style**: Flat vector poster, NOT photorealistic
- **Palette**: DraftGuru brand colors (primary blue #4A7FB8, peach #E8B4A8, cyan accent #06b6d4)
- **Skin Tones**: 3-tone warm ramp matching player's complexion
- **Clothing**: Generic jersey, no logos/numbers
- **Hard Exclusions**: No text, watermarks, team logos, busy backgrounds

Prompt versions can be tracked via `system_prompt_version` field for A/B testing.

---

## Future Enhancements

1. **Admin UI** - View/regenerate images with prompt tweaking
2. **Batch Scheduling** - Cron job for new draft prospects
3. **Quality Scoring** - Auto-detect failed/low-quality generations
4. **CDN Integration** - CloudFront for global distribution
5. **Multiple Poses** - Action shots, jersey variants
