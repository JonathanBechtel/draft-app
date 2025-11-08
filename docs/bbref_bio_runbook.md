# Basketball-Reference Bio Ingestion — Runbook

This document summarizes the bio enrichment implementation, how to run it, and how the data maps into the schema.

## Overview

- Source: Basketball-Reference (BRef) player index and player pages.
- Goals: enrich immutable player facts in `players_master`, populate ephemeral status in `player_status`, persist audit snapshots, and seed external IDs (BRef slug + socials).
- Outputs: a CSV export and a database ingestion path that is idempotent and safe to re-run.

## Components

- Scraper: `scripts/bbref_bio_scraper.py`
  - Produces CSV: `scraper/output/bbio_<scope>_<YYYYMMDD>.csv`
  - Caches HTML to `scraper/cache/players_{letter}.html` and `scraper/cache/players/{slug}.html`
  - Supports offline parsing from sample HTML in `scrapers/bbref/`
- Ingestor: `scripts/ingest_player_bios.py`
  - Resolves to `players_master.id` using external IDs, aliases, or deterministic name rules
  - Upserts `player_external_ids` (systems: `bbr`, `x`, `instagram`)
  - Updates immutable facts on `players_master` (fill-when-null by default)
  - Upserts `player_status` (active flag, team, position, height, weight)
  - Inserts `player_bio_snapshots` with raw HTML (if present in cache)
- Make targets (see Makefile)
  - `bio.scrape` and `bio.ingest`

## Schema Changes

- `app/schemas/players_master.py`
  - Added immutable fields: `birth_city`, `birth_state_province`, `birth_country`, `school`, `high_school`, `shoots`, `draft_year`, `draft_round`, `draft_pick`, `draft_team`, `nba_debut_date`, `nba_debut_season`.
- `app/schemas/player_status.py` (new)
  - Ephemeral fields: `is_active_nba`, `current_team`, `nba_last_season`, `position`, `height_in`, `weight_lb`, `source`, `updated_at`.
- `app/schemas/player_bio_snapshots.py` (new)
  - Audit: `player_id`, `source`, `source_url`, `scraped_at`, `raw_meta_html`.
- `app/schemas/player_external_ids.py`
  - Reused to store: `bbr` slugs, `x` handles, `instagram` handles.

Generate the Alembic migration and apply it before first ingest:

- `make mig.revision m="feat: player_status + bio_snapshots + master bio fields"`
- `make mig.up`

## Dependencies

- BeautifulSoup is included in both `pyproject.toml` and `environment.yml`.
- Update your environment if needed:
  - Conda: `conda env update -f environment.yml && conda activate draftguru`
  - Pip: `pip install -e .[dev]`

## Scraping (Live and Offline)

- Live (one letter):
  - `make bio.scrape LETTERS=b OUT=scraper/output THROTTLE=4`
- Live (multiple):
  - `make bio.scrape LETTERS=a,b,c OUT=scraper/output THROTTLE=4`
- Live (all letters; long-running):
  - `make bio.scrape ALL=1 OUT=scraper/output THROTTLE=4`
- Offline (use samples in `scrapers/bbref/`):
  - `make bio.scrape LETTERS=b FROM_INDEX_FILE=scrapers/bbref/index_page_example.html FROM_PLAYER_FILE=scrapers/bbref/player_page_example.html OUT=scraper/output`

Notes
- Throttling: a sleep between requests is built in; set `THROTTLE` to `>=3` seconds to honor BRef guidance. Default is 3s.
- User-Agent: `draftguru-bio-scraper/0.1`.
- Caching: pages are stored under `scraper/cache/` to keep reruns deterministic and reduce network load.

## CSV Schema (Export)

Columns written by the scraper:
- Identity: `slug`, `url`, `full_name`, `scraped_at`, `source_url`
- Immutable: `birth_date`, `birth_city`, `birth_state_province`, `birth_country`, `shoots`, `school`, `high_school`, `draft_year`, `draft_round`, `draft_pick`, `draft_team`, `nba_debut_date`, `nba_debut_season`
- Ephemeral: `is_active_nba`, `current_team`, `nba_last_season`, `position`, `height_in`, `weight_lb`
- Socials: `social_x_handle`, `social_x_url`, `social_instagram_handle`, `social_instagram_url`

## Ingestion

- Dry-run first:
  - `make bio.ingest BBIO="$(ls -t scraper/output/bbio_*.csv | head -n1)" DRY=1 VERBOSE=1`
- Commit:
  - `make bio.ingest BBIO="$(ls -t scraper/output/bbio_*.csv | head -n1)" VERBOSE=1`

Behavior
- Resolution order: external ID `bbr` → alias exact → deterministic rule-based match (last exact + first exact/initial; ambiguous names are reported).
- `CREATE_MISSING`: enabled by default to create `players_master` rows for unmatched bios.
  - Disable for a run by passing `CREATE_MISSING=` to the `make` call.
- Overwriting immutable facts: off by default; use `OVERWRITE_MASTER=1` if you are correcting data.
- Ambiguities: ingestor writes `bbio_ambiguous.json` and `bbio_unmatched.json` next to the CSV. You can re-run with `--fix-ambiguities` by passing `FIX=path/to/fixes.json` to the make target.

What is written
- `players_master`: fills immutable facts only when `NULL` (unless `--overwrite-master`).
- `player_status`: upsert/replace with latest ephemeral values.
- `player_external_ids`: adds rows for `bbr` slug and socials (`x`, `instagram`).
- `player_aliases`: adds display-name alias if missing.
- `player_bio_snapshots`: saves `div#meta` HTML when the player page exists in cache.

## Scope and Filters

- Scraper exports rows for chosen letters; active players are marked.
- Ingest attaches only rows resolvable to `players_master` or creates them when `CREATE_MISSING` is on (default).

## Troubleshooting

- Missing CSV: ensure `bbio_<scope>_<YYYYMMDD>.csv` exists in `scraper/output/`.
- Migration errors (relation does not exist): run `make mig.up`.
- Rate limiting: increase `THROTTLE` (e.g., `THROTTLE=5`) or run in smaller letter batches.
- Wrong birthplace formatting (older CSVs): ingestion normalizes `birth_state_province` and `birth_country`. Re-scrape to fix values in CSV too.

## Future Enhancements

- Optional `only_active` ingest flag to restrict create/update to active NBA players.
- Weekly roster refresh by re-scraping index pages at a safe cadence.
- Promote socials to a dedicated `player_social_accounts` table when we start building feed ingestion.

