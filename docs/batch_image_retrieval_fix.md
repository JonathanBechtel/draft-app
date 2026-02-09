# Fix: Batch Image Retrieval — Metadata-Based Correlation

## Problem

Batch image generation via the Gemini API was failing at the retrieval step. The code relied on injecting a `DG_REQUEST_ID` into the user prompt and asking Gemini to echo it back as a JSON text part alongside the generated image. This never worked — image generation models don't produce structured text alongside images. Multiple attempts to fix the text-based correlation failed (commits `526a962`, `f13d386`, etc.).

The prompt also included `response_mime_type="application/json"`, which actively conflicted with image generation by telling the model to produce JSON output rather than an image.

As a result, the retrieval code could never correlate responses back to players, and every batch job either errored out or refused to ingest.

## Root Cause

The correlation strategy was fundamentally flawed: it assumed the model would echo back a request ID in a text part, but image generation endpoints return image data, not structured JSON.

## Solution

Research confirmed two key facts about the Gemini batch API:

1. **Inline batch response order is guaranteed** — `inlinedResponses[i]` corresponds to `requests[i]`
2. **`metadata` on `InlinedRequest` is echoed back on `InlinedResponse`** — support added in `google-genai` SDK v1.61.0 (2026-01-30)

The fix uses a two-tier correlation strategy:

- **Primary:** Read `resp.metadata["player_id"]` from the echoed response metadata
- **Fallback:** Positional index matching against the ordered request records list (handles legacy jobs or older SDK versions)

## Changes

### SDK Upgrade

`google-genai` upgraded from 1.56.0 to 1.62.0 to get metadata echo support.

### `app/services/image_generation.py`

**`build_batch_request`**
- Removed prompt injection that embedded `DG_REQUEST_ID` and asked Gemini to echo it back as JSON
- Removed `response_mime_type="application/json"` from config (conflicted with image generation)
- Kept `metadata={"player_id": ..., "dg_request_id": ...}` on `InlinedRequest` — this is the real correlation mechanism

**`retrieve_batch_results`**
- Replaced text-based JSON parsing correlation block with metadata-based + positional fallback
- Removed early-exit guard that refused to ingest jobs without `dg_request_id` in `player_ids_json` — positional matching handles legacy jobs
- Added logging for which correlation method is used per response

**`_extract_and_upload`**
- Removed `dg_request_id` parameter
- Removed the line that polluted the stored `user_prompt` with a `DG_REQUEST_ID:` prefix
- Removed `dg_request_id` from S3 upload metadata
