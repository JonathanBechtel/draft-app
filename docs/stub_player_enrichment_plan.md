# Stub Player Auto-Enrichment Plan

## Problem

When news tagging creates a stub player via `_create_stub_player()`, the resulting profile has only a name and `is_stub=True`. Empty profiles are a poor user experience and clutter the app with pages no one wants to visit.

## Goal

When a new stub player is created, automatically kick off a background enrichment pipeline that:

1. Fills in biographical details (school, birthdate, hometown, position, measurements)
2. Finds a Creative Commons-licensed reference photo
3. Generates a DraftGuru-style player portrait image
4. Collects basic college stats (PPG, RPG, APG, etc.)

## Architecture Overview

```
Stub Created (_create_stub_player)
  │
  ├─► Stage 1: Bio + Stats Enrichment (Gemini Flash + Google Search grounding)
  │     - Biographical details
  │     - Basic college stats
  │     - Likeness description (for image gen)
  │
  ├─► Stage 2: Reference Image Discovery (Wikimedia Commons API)
  │     - CC-licensed photo search
  │     - Store URL in reference_image_url
  │
  └─► Stage 3: Player Image Generation (existing Gemini image pipeline)
        - Depends on Stage 1 (likeness description) and Stage 2 (reference image)
        - Uses existing synchronous generate_content() path
```

Stages 1 and 2 run concurrently. Stage 3 runs after both complete (but proceeds with whatever data is available if one fails).

## Stage 1: Bio + Stats Enrichment via Gemini with Google Search

### Approach

Use `gemini-3-flash-preview` with the `google_search` tool enabled for grounding. This lets Gemini search the web for current, accurate information rather than relying on training data.

```python
from google.genai import types

response = client.models.generate_content(
    model="gemini-3-flash-preview",
    contents="Find biographical details and college stats for {player_name}, {draft_year} NBA Draft prospect",
    config=types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        response_mime_type="application/json",
        response_schema=BioEnrichmentSchema,
    ),
)
```

### Data Extracted

**Biographical (maps to PlayerMaster + PlayerStatus):**
- `birthdate`, `birth_city`, `birth_state_province`, `birth_country`
- `school`, `high_school`
- `height_inches`, `weight_lbs`
- `position`, `shoots`
- `likeness_description` (physical description for image generation)

**Stats (new lightweight model or JSON field):**
- PPG, RPG, APG, FG%, 3P%, FT%
- Games played, season

**Metadata:**
- `confidence`: `high` | `medium` | `low` (from prompt instruction)
- `grounding_sources`: list of URLs Gemini cited

### Confidence Gating

- Gemini returns a confidence level as part of the structured response
- Only persist bio data for `high` or `medium` confidence
- Mark AI-populated fields with `bio_source = "ai_generated"` so admins can review/override
- `is_stub` remains `True` until an admin verifies or the player appears in official combine data

### Cost

Google Search grounding: ~$35 per 1,000 grounded requests on Flash. At draft-class volume (~100-200 players/year), this is a few dollars total.

## Stage 2: Reference Image Discovery via Wikimedia Commons

### Approach

Query the Wikimedia Commons API for CC-licensed photos:

```
GET https://commons.wikimedia.org/w/api.php
  ?action=query
  &list=search
  &srsearch="{player_name}" basketball
  &srnamespace=6
  &format=json
```

### Process

1. Search Wikimedia Commons for player name + "basketball"
2. Fetch file info for top results to get direct URL and license metadata
3. Only accept: CC-BY, CC-BY-SA, CC0, or Public Domain
4. Store the URL in `PlayerMaster.reference_image_url`

### Fallbacks

- If no Wikimedia result, try the Wikipedia article infobox image (almost always CC-licensed)
- If nothing found, skip gracefully -- image generation can still work without a reference photo (just less accurate likeness)

### Why Wikimedia?

- Proper API with license metadata built in
- No scraping or ToS issues
- Good coverage of college basketball players
- Rate limits are generous

## Stage 3: Player Image Generation

### Approach

Use the existing `image_generation.py` pipeline (synchronous `generate_content()` path) to generate a single DraftGuru-style portrait.

### Inputs

- `reference_image_url` from Stage 2 (if available)
- `likeness_description` from Stage 1 (if available)
- Player name for the user prompt

### Why Synchronous?

- It's a single image, not a batch
- User experience benefit of having the image ready quickly outweighs the 50% batch discount
- Keeps the pipeline simple

## Schema Changes

### PlayerMaster additions

```python
bio_source: Optional[str] = Field(default=None)  # "manual" | "ai_generated" | "verified"
enrichment_attempted_at: Optional[datetime] = Field(default=None)
```

`bio_source` tracks how bio data was populated. `enrichment_attempted_at` prevents retrying players that already went through the pipeline (whether it succeeded or not).

### New table: EnrichmentLog (optional)

Tracks each enrichment attempt for debugging/auditing:

- `player_id`, `stage` (bio, image_search, image_gen)
- `status` (success, failure, skipped)
- `source_urls` (JSON list of grounding sources)
- `error_message`
- `created_at`

## Execution Model

### Primary: Inline async during ingestion

After `_persist_player_mentions()` creates stubs, fire `asyncio.create_task()` for each new stub:

```python
# In player_mention_service.py, after stub creation
new_stub_ids = [...]  # IDs of newly created stubs
for player_id in new_stub_ids:
    asyncio.create_task(enrich_stub_player(player_id))
```

The ingestion cycle doesn't wait for enrichment to complete.

### Fallback: Cron sweep

Add a step to `cron_runner.py` that queries for `is_stub=True` players where `enrichment_attempted_at IS NULL`, and processes them. This catches any that failed or were missed during ingestion.

## Implementation Order

1. Add `bio_source` and `enrichment_attempted_at` fields to PlayerMaster + Alembic migration
2. Create `app/services/player_enrichment_service.py` with Stage 1 (bio + stats via grounded Gemini)
3. Add Stage 2 (Wikimedia Commons image search)
4. Wire Stage 3 (image generation using existing pipeline)
5. Hook into `_persist_player_mentions()` to trigger on stub creation
6. Add cron sweep to `cron_runner.py` as safety net
7. Add enrichment admin view (list of AI-enriched players pending review)

## Risk Mitigation

| Concern | Mitigation |
|---|---|
| Gemini hallucinating bio data | Google Search grounding + confidence field + `bio_source="ai_generated"` flag |
| No CC image found | Graceful skip; image gen works without reference |
| Wikimedia rate limits | Generous limits; add backoff + cron retry |
| Enrichment fails mid-pipeline | Each stage independent; partial enrichment is fine |
| Duplicate enrichment attempts | `enrichment_attempted_at` timestamp prevents retries |
| Cost | ~$0.04/player for grounded Gemini + image gen; negligible at draft-class volume |

## Files to Create/Modify

**New:**
- `app/services/player_enrichment_service.py` -- orchestrates all 3 stages
- `app/schemas/enrichment_log.py` -- (optional) audit table
- Alembic migration for new fields

**Modified:**
- `app/schemas/players_master.py` -- add `bio_source`, `enrichment_attempted_at`
- `app/services/player_mention_service.py` -- fire enrichment after stub creation
- `app/cli/cron_runner.py` -- add enrichment sweep step
