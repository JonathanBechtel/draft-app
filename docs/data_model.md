# Data Model Rationale and Source Linking

This note explains why the combine schema is structured the way it is and how different ingestion sources are linked to a single, canonical player identity.

## Overview

The goals driving the schema:
- Provide a canonical `player_id` that unifies records across multiple sources and seasons.
- Store NBA Draft Combine data (Anthropometrics, Strength & Agility, Shooting) in season‑scoped facts.
- Normalize shooting to one row per drill to avoid schema churn if drills change.
- Keep ingestion idempotent and auditable, while remaining simple to extend for future sources.

Key tables (authoritative SQLModel definitions live under `app/schemas/`):
- Identity core
  - `players_master`: canonical player record (name parts, display name).
  - `player_aliases`: known name variants, one per player; used for matching.
  - `player_external_ids`: stable external IDs (e.g., NBA Stats `PLAYER_ID`).
  - `seasons`: canonical season codes (`YYYY-YY`).
- Combine facts
  - `combine_anthro`: one row per (player, season).
  - `combine_agility`: one row per (player, season).
  - `combine_shooting_results`: one row per (player, season, drill).

## Why a Canonical Player Table

- Multi‑source reality: names differ across sources (order, suffixes, diacritics). A dedicated canonical table decouples ingestion from display and makes later merges/edits possible without rewriting fact tables.
- Stable joins: every fact table references the same `players_master.id` FK, so downstream queries can combine sources reliably.
- Future‑proofing: when adding production stats, bios, or external rosters, link via `player_external_ids` or add new alias rows without altering facts.

## How Sources Link to Players

Ingestion resolves each row to a `players_master` record using:
1) External ID (preferred): if a payload contains a stable external ID (e.g., NBA Stats `PLAYER_ID`), match via `player_external_ids (system='nba_stats')`.
2) Alias match: build a full alias from name parts (prefix/first/middle/last/suffix) and match against `player_aliases.full_name`.
3) Create‑on‑missing: if neither match succeeds, create a new `players_master` row, seed a `player_aliases` row with the alias, and seed `player_external_ids` when the source provides one.

Every fact row also stores `raw_player_name` (and optional `nba_stats_player_id`) for audit and backfills.

## Seasons as First‑Class

- `seasons` holds `code` (e.g., `2024-25`) and derived `start_year`/`end_year`. Facts reference seasons by FK.
- This removes string duplication and makes filtering/indexing on season efficient.

## Shooting: Why Normalized

- The NBA shooting table groups multiple drills. Encoding those in columns forces schema edits when drills change.
- A normalized table (`combine_shooting_results`) uses `drill` + `fgm/fga`:
  - Supported drills today: `off_dribble`, `spot_up`, `three_point_star`, `midrange_star`, `three_point_side`, `midrange_side`, `free_throw`.
- Benefits: simpler ingestion, easier expansion, and straightforward comparison queries.

## Idempotency and Constraints

- Uniqueness prevents duplicates:
  - `combine_anthro`: unique (player_id, season_id)
  - `combine_agility`: unique (player_id, season_id)
  - `combine_shooting_results`: unique (player_id, season_id, drill)
- Ingestion performs upserts keyed by those unique tuples.
- Common filter indexes exist on `(season_id)` and `(pos)`, and `(drill)` for shooting.

## Units and Normalizations

- Lengths in inches (anthro heights/wingspan/reach; agility verticals).
- Times in seconds; bench as integer reps; body fat as percentage (0–100).
- `pos` is preserved exactly as provided (e.g., `SG-SF`).

## Example: Stitching Sources

- Anthro + Agility for one season:
  - Join `combine_anthro` and `combine_agility` on `(player_id, season_id)`.
- Shooting vs Anthro for wingspan correlation:
  - Aggregate shooting by `(player_id, season_id)` and join to `combine_anthro` on the same keys.
- Cross‑season trend for one player:
  - Filter by a single `player_id` and join multiple seasons via `seasons.code`.

## Handling Name Collisions and Corrections

- If two real‑world players share the same alias, prefer adding/using external IDs to disambiguate.
- If an alias was attached to the wrong player, add the correct external ID (or alias) and re‑ingest; future rows will link correctly. Existing fact rows can be corrected with a targeted FK update if needed.

## Adding New Sources

- Prefer direct ingestion into fact tables when the payload is clean and keyed by season/player.
- If parsing is noisy, consider a transient staging import (CSV → staging) with a subsequent merge into facts; then drop the staging when done.
- Extend identity by adding new `player_external_ids` systems (e.g., `bbr`, `espn`) so future ingests can anchor on stable IDs.

## Migrations Guidance

- Author schema under `app/schemas/*`.
- Create migrations with Alembic autogenerate and review edits:
  - `alembic revision --autogenerate -m "<message>"`
  - `alembic upgrade head`
- Avoid calling `create_all()` inside migration files; revisions should be frozen snapshots independent of model imports.

## Ingestion Workflow (Dev → Prod)

- Generate or refresh CSVs: `make scrape YEAR=2024-25` (or all seasons).
- Apply migrations: `alembic upgrade head`.
- Ingest into dev (staging): `make ingest` (or filter with `YEAR` / `SOURCE`).
- Promote to prod by pointing `DATABASE_URL` to prod and re‑running `alembic upgrade head` + `make ingest`.

## Trade‑Offs and Future Work

- `drill` is a text field for flexibility; if the set stabilizes, we can migrate to an enum with a lookup table.
- We intentionally skipped a `player_bio` table for now; once a bio source is chosen, it can reference `players_master.id` without touching existing facts.
- If identity merges/splits become frequent, we can add admin tooling to reassign facts between `player_id`s safely.
