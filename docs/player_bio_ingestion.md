# Player Bio Ingestion Plan

This note outlines a one-time ingestion pass to enrich player records with Basketball-Reference data and identify NBA participation flags without introducing a recurring job.

## Objectives

- Enrich each `Player` / `PlayerMaster` record with birth details, education history, and other static bio facts that inform scouting metrics.
- Tag players who are *currently on an NBA roster* and players who have *ever* appeared in an NBA regular-season game.
- Keep scope limited to a single pull that can be re-run manually when needed; no scheduled jobs or paid data sources.

## Scope & Data Points

We care about data that is both freely available and relatively static. For each player we aim to collect:

- Birth date, age sanity check, and birthplace (city, state, country).
- School/college (or `None` when the prospect is international).
- Height/weight snapshots, draft pick, and position where available (used for QA even if already stored).
- NBA career span: first and last season, total games, and indicator for current roster status.

These fields extend the existing `players` model (`has_nba_experience`, `is_active_nba`, `nba_debut_season`, `nba_last_season`, `birth_place`, `college`) plus a raw snapshot table for source auditing.

## Primary Source: Basketball-Reference

Basketball-Reference (BRef) exposes the needed data via static HTML pages:

- Player index pages (`https://www.basketball-reference.com/players/{letter}/`) list every player whose last name starts with `{letter}` and include birth year, college, and NBA seasons. Current players are marked with an asterisk.
- Individual player pages add birthplace, draft info, and biographical sections.

Why BRef works well:

- Pages are server-side rendered (no headless browser required). `requests` + BeautifulSoup or `pandas.read_html` can parse the tables once HTML comments are stripped.
- Rate limits are manageable (respect the published guidance of one request every 3–5 seconds, rotate a descriptive `User-Agent`).
- Data is long-lived and rarely removed, making it ideal for a one-time enrichment pass.

### Robots.txt Considerations

- Respect the published `Crawl-delay: 3` by sleeping at least three seconds between requests from a single job.
- Only fetch paths that are allowed for `User-agent: *`; avoid disallowed directories such as `/play-index/`, `/dump/`, `*/gamelog/`, and related query endpoints.
- Document the user agent string we send (e.g., `draftguru-bio-scraper/0.1`) so we can trace traffic if they contact us.

## Supporting Sources (Manual Fallbacks)

Use these only when BRef lacks a field:

- NBA.com Stats JSON endpoints (`commonplayerinfo`, `playerprofilev2`) for roster flags or draft data on newer players without an indexed BRef page yet.
- Wikipedia / Wikidata API for missing schools or birthplace details, especially for prospects who have yet to log an NBA game.

## Implementation Outline

1. **Prepare player identity mapping**
   - Ensure every `PlayerMaster` row has either an NBA Stats ID, BRef slug, or a reliable alias (existing `player_external_ids`/`player_aliases` tables).
   - For players without a BRef slug, generate one using normalized names (`lastname + first two letters of first name + 01`) and confirm manually if necessary.
   - Persist BRef slugs in `player_external_ids` with `system='basketball_reference'` so downstream imports can join by ID instead of name.

### Linking to the Data Model

- The ingestion script should resolve each scraped record to a `PlayerMaster.id` by first matching on existing external IDs (`player_external_ids`). If no match is found, fall back to the `player_aliases` table (case-folded full name) before considering fuzzy matching.
- When a new match is confirmed, upsert both the external ID row and a canonical alias so future runs (or other data feeds) do not need to repeat the resolution work.
- Keep a small review queue (e.g., JSON report) for ambiguous matches—cases where multiple `PlayerMaster` rows share a surname/initial combination or where the slug pattern produces conflicting results. Resolve these manually and append the decision to `player_aliases`.
- If a player is missing from `PlayerMaster`, create the row first (populate basic name fields) and then attach the scraped bio + external IDs. This keeps `Player` (the persistence table) and `PlayerMaster` (identity) in sync.

2. **Scrape BRef index pages**
   - Iterate letters `a`–`z` (and `players/` special cases such as "_" for non-alphabetic surnames).
   - For each row, extract: slug, player name, position, birth year, debut/final season, college, active indicator (asterisk).
   - Cache the raw HTML under `scraper/cache/players_{letter}.html` to aid debugging and keep re-runs deterministic.
   - Throttle requests with `time.sleep(3)` (or higher) and send the documented user agent to comply with robots guidance.
   - Some players—especially recent draft picks or those who changed their last name—have slugs whose first letter no longer matches the index bucket (e.g., `artesro01` listed under "W"). The scraper now resolves URLs by slug instead of the letter, but if a player is missing entirely from the index you scraped, pass their slug via `EXTRA_SLUGS=slug1,slug2` (or `EXTRA_SLUGS_FILE=...`) so their player page is still fetched and exported.

3. **Fetch detailed bios**
   - For each slug matched to a `PlayerMaster`, download the player page.
   - Parse the bio box (`div#meta`) for birthplace, height, weight, shoot hand, draft round/pick, and college list. Normalize birthplace into `city`, `state_province`, `country` fields.
   - Record a parsing timestamp and store the raw text/HTML snippet in a `PlayerBioSnapshot` table for audit.

4. **Upsert into the database**
   - Extend `app/models/players.py` and `app/schemas/players.py` with new nullable columns for the enriched fields. Provide a migration that adds these columns to `players` plus `player_nba_seasons` if we decide to keep season-level detail.
   - Write an ingestion script under `scripts/ingest_player_bios.py` that:
     1. Loads cached or freshly downloaded BRef data.
     2. Resolves each row to a `PlayerMaster` and `Player` record via external ID or alias.
     3. Updates the denormalized flags and bios on the `players` table.
     4. Inserts/updates `PlayerBioSnapshot` for raw data provenance.
     5. Optionally populates `player_nba_seasons` with `(player_id, season, team, active_flag)` rows to support future queries.

5. **Validate results**
   - Add pytest coverage: fixture HTML for index + detail pages, unit tests for parsing functions, and integration tests that confirm upserts set `has_nba_experience` and `is_active_nba` correctly.
   - Run data QA reports (e.g., list players with missing birth dates, duplicate colleges, mismatched active flags) before accepting the update.

## Running the One-Time Ingestion

- Activate the project environment (`conda activate draftguru`).
- Execute the script in dry-run mode to verify parsing (`python scripts/ingest_player_bios.py --file <path/to/bbio.csv> --cache-dir scraper/cache/players --dry-run`).
- Inspect the generated cache files and logs; spot-check a few players against BRef manually.
- Rerun without `--dry-run` to commit updates to the database. Provide command-line options for `--letters a,b,c`, `--player-id 123`, or `--slug lebronj01` so partial reruns are easy.
- The `bio.ingest` make target automatically points to the newest `bbio_*.csv` in `OUT`; pass `BBIO=...` only when you need to ingest an older export.

Because the data is mostly static, document the run (date, source commit, database target) in `docs/runbook.md` or add an entry to `docs/v_1_roadmap.md` so we remember when the bios were last refreshed.

## Future Considerations

- If metrics later need always-fresh roster status, we can revive this script as a scheduled task or hook into NBA.com rosters weekly. For now, the manual rerun path is sufficient.
- Paid APIs (SportsRadar, SportsDataIQ) remain an option once the project needs guaranteed uptime or extended stats; the ingestion script can be modularized to accept alternative providers.
