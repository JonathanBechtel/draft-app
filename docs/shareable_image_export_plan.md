# Shareable Image Export Service - Implementation Plan

## Project Overview

**Linear Project:** [Add Image Generation Service for Easy Sharing of Application Visuals](https://linear.app/draftguru/project/add-image-generation-service-for-easy-sharing-of-application-visuals-2850dfe12246)

**Initiative:** Product

**Goal:** Enable users to download branded PNG images of analytics sections for sharing on Twitter and Reddit, driving organic traffic back to nbadraft.app.

---

## Technical Approach: Server-Side Rendering

### Why Server-Side?

| Requirement | Client-Side | Server-Side |
|-------------|-------------|-------------|
| Russo One / Azeret Mono fonts | Unreliable loading | Preloaded, guaranteed |
| S3 player images | CORS issues | Direct fetch, no issues |
| Scanline overlays, pixel corners | Inconsistent rendering | Pixel-perfect |
| Brand watermark placement | Varies by browser | Exact positioning |
| Color palette accuracy | Browser-dependent | Controlled environment |

**Decision:** Use **Playwright** for server-side rendering.

### Output Specification

- **Format:** PNG (lossless, preserves text/stat clarity)
- **Dimensions:** 1200×630 (2:1 ratio, optimal for Twitter/Reddit)

---

## Target Components

Four sections only:

| # | Component | Page |
|---|-----------|------|
| 1 | **VS Arena** | Homepage |
| 2 | **Performance Metrics** | Player Detail |
| 3 | **Head-to-Head** | Player Detail |
| 4 | **Player Comparisons** | Player Detail |

---

## Image Layout

```
┌────────────────────────────────────────────────────────────────┐
│                                                                │
│  COOPER FLAGG vs DYLAN HARPER                         ← Title  │
│  Current Draft · Same Position · Combine Performance  ← Context│
│  ════════════════════════════════════════════                  │
│                                                                │
│                     [Analytics Content]                        │
│                                                                │
├────────────────────────────────────────────────────────────────┤
│  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  nbadraft.app  ░░░░ │ ← Watermark
└────────────────────────────────────────────────────────────────┘
```

### Title by Component

| Component | Title Format |
|-----------|--------------|
| VS Arena | `{Player A} vs {Player B}` |
| Performance Metrics | `{Player} - Performance Percentiles` |
| Head-to-Head | `{Player A} vs {Player B}` |
| Player Comparisons | `{Player} - NBA Comparisons` |

### Context Line

Shows active filters separated by middle dots:

- **Comparison Group:** "Current NBA Players" | "Current Draft Class" | "All-Time Draft Prospects"
- **Position Filter:** "Same Position" | "All Positions"
- **Metrics** (Performance only): "Anthropometrics" | "Combine Performance" | "Shooting"

Example: `Current Draft Class · Same Position · Combine Performance`

---

## User Experience

### Modal Preview
1. User clicks "Save as Image" button
2. Loading spinner appears
3. Modal opens with image preview
4. Download button triggers browser save

### Button Placement
- Icon button in top-right corner of each section
- Both mobile and desktop
- Tooltip: "Save as Image"

---

## Service Design (Reusability)

The export service is designed to be **component-agnostic**. Adding new exportable sections requires only:
1. A template in `templates/export/`
2. Registration in the component registry

```python
@dataclass
class ExportComponent:
    """Definition for an exportable component."""
    template: str                              # Jinja template path
    title_fn: Callable[[dict], str]            # Generates title from context
    context_fn: Callable[[dict], str]          # Generates context line
    cache_prefix: str                          # S3 subfolder

class ImageExportService:
    """
    Reusable export service.

    New sections just register here - no changes to core render logic.
    """

    COMPONENTS: dict[str, ExportComponent] = {
        "vs_arena": ExportComponent(
            template="export/vs_arena.html",
            title_fn=lambda ctx: f"{ctx['player_a_name']} vs {ctx['player_b_name']}",
            context_fn=build_comparison_context,
            cache_prefix="vs-arena",
        ),
        "performance": ExportComponent(
            template="export/performance.html",
            title_fn=lambda ctx: f"{ctx['player_name']} - Performance Percentiles",
            context_fn=build_metrics_context,
            cache_prefix="performance",
        ),
        # Future sections just add entries here
    }

    async def export(self, component: str, player_ids: list[int], context: dict) -> ExportResult:
        """Export any registered component - same interface for all."""
        if component not in self.COMPONENTS:
            raise ValueError(f"Unknown component: {component}")

        comp = self.COMPONENTS[component]
        cache_key = self._build_cache_key(comp, player_ids, context)

        # Check cache first
        cached_url = await self._check_cache(comp.cache_prefix, cache_key)
        if cached_url:
            return ExportResult(url=cached_url, cached=True)

        # Render and cache
        url = await self._render_and_upload(comp, player_ids, context, cache_key)
        return ExportResult(url=url, cached=False)
```

This design means:
- **No code changes** to add new exportable sections
- **Consistent behavior** across all exports (caching, watermarks, dimensions)
- **Easy testing** via component registration mocking

---

## Architecture

```
User clicks "Save as Image"
        │
        ▼
POST /api/export/image
{
  component: "h2h",
  player_ids: [123, 456],
  context: { comparison_group, same_position, metrics }
}
        │
        ▼
┌─────────────────────────┐
│   ImageExportService    │
│  1. Check S3 cache      │
│  2. If miss → render    │
│  3. Return URL          │
└─────────────────────────┘
        │
        ▼
┌─────────────────────────┐
│     RenderService       │
│  1. Playwright browser  │
│  2. Load render template│
│  3. Screenshot          │
│  4. Upload to S3        │
└─────────────────────────┘
        │
        ▼
Modal shows preview + download button
```

### S3 Cache Structure

```
s3://{bucket}/exports/
├── vs-arena/{player_a}_{player_b}_{hash}.png
├── performance/{player}_{hash}.png
├── h2h/{player_a}_{player_b}_{hash}.png
└── comps/{player}_{hash}.png
```

Cache TTL: 24 hours

---

## Pre-Generation

Nightly job generates exports for top 30 prospects:
- Performance Metrics (all 3 metric categories)
- Player Comparisons
- Common H2H matchups (top 10 × top 10)

---

## File Structure

```
app/
├── routes/
│   ├── export.py              # POST /api/export/image
│   └── internal_render.py     # GET /internal/render/{component}
├── services/
│   ├── image_export_service.py
│   └── render_service.py
├── templates/export/
│   ├── base_export.html       # Fonts, viewport, watermark
│   ├── vs_arena.html
│   ├── performance.html
│   ├── h2h.html
│   └── comps.html
├── static/
│   ├── css/export.css
│   └── js/export-modal.js
└── utils/
    └── export_cache.py
```

---

## Implementation Steps

### Phase 0: Infrastructure
- [ ] Add Playwright to environment.yml
- [ ] Add `EXPORT_CACHE_PREFIX` config
- [ ] Create `/internal/render/` routes

### Phase 1: Core Service
- [ ] `RenderService` with Playwright browser pool
- [ ] `base_export.html` (fonts, 1200×630 viewport, watermark)
- [ ] S3 cache layer
- [ ] `ImageExportService`
- [ ] `/api/export/image` endpoint

### Phase 2: Frontend
- [ ] `export-modal.js` for preview modal
- [ ] "Save as Image" button component
- [ ] Integrate into all 4 sections (VS Arena, Performance, H2H, Comps)
- [ ] Mobile + desktop support

### Phase 3: Templates
- [ ] `vs_arena.html`
- [ ] `performance.html`
- [ ] `h2h.html`
- [ ] `comps.html`
- [ ] `export.css`

### Phase 4: Polish
- [ ] Loading states
- [ ] Error handling
- [ ] Pre-generation script
- [ ] Rate limiting

---

## Style Guide Compliance

Per `docs/style_guide.md`:

- **Title:** Russo One, uppercase
- **Context:** Azeret Mono, muted
- **Data:** Azeret Mono tabular
- **Watermark:** Slate-800 bar, "nbadraft.app" in white Azeret Mono

---

## API

### POST /api/export/image

**Request:**
```json
{
  "component": "h2h",
  "player_ids": [1661, 1587],
  "context": {
    "comparison_group": "current_draft",
    "same_position": true,
    "metrics": "combine"
  }
}
```

**Response:**
```json
{
  "url": "https://.../exports/h2h/1587_1661_a3f8.png",
  "title": "Cooper Flagg vs Dylan Harper",
  "filename": "cooper-flagg-vs-dylan-harper.png"
}
```

---

## Dependencies

- **playwright**: Headless browser
- **Pillow** (optional): Post-processing
