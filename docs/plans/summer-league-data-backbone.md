# Summer League Data Backbone

## Purpose

Build the durable data backbone for DraftGuru's Summer League stats product.
This project starts from the completed NBA.com Stats raw scrape and turns it
into an auditable, repeatable, product-grade database foundation.

The immediate goal is not public UI. The goal is to make the data trustworthy
enough that future Summer League pages, leaderboards, player sections,
explorers, share cards, and composite metrics can be built without re-scraping
or reinterpreting raw NBA.com payloads.

## Product Context

DraftGuru wants to become a lightweight Basketball-Reference-style archive for
NBA Summer League. The raw scrape already captured NBA.com Stats snapshots under
`data/raw/nba_stats/summer_league/`, with manifests covering Vegas, Orlando,
California Classic, and Salt Lake City across historical years.

The next architecture step is:

```text
raw files -> durable archive -> raw audit metadata -> normalized facts
          -> player resolution -> derived/read models -> product pages
```

This spec covers the first five stages only.

## Goals

- Preserve the completed raw scrape as a durable source artifact.
- Record auditable metadata for every raw run and raw file.
- Create normalized Summer League product tables with stable NBA.com IDs.
- Normalize competitions, teams, games, source players, and box-score facts.
- Resolve NBA.com source players to DraftGuru canonical `players_master` rows.
- Support unresolved and ambiguous players without blocking stat ingestion.
- Provide an end-to-end QA harness that stress tests the full pipeline.
- Make every step idempotent and rerunnable.

## Non-Goals

- No public Summer League routes or templates.
- No leaderboard/read-model UI.
- No Explorer page.
- No share cards.
- No composite stat formulas beyond directly parsed box/advanced endpoint facts.
- No live polling or scheduled production job.
- No admin UI for player-resolution review in the first pass, unless explicitly
  pulled forward.

## Source Data

Raw files currently live under:

```text
data/raw/nba_stats/summer_league/{year}/{league_id}/
  manifest.json
  leaguegamelog_team.json
  leaguegamelog_player.json
  games/
    {game_id}/
      boxscoretraditionalv2.json
      boxscoreadvancedv2.json
      boxscorescoringv2.json
      playbyplayv2.json
      shotchartdetail.json
```

The durable archive should preserve this relative layout exactly. A recommended
S3 key shape is:

```text
s3://<bucket>/raw/nba_stats/summer_league/{year}/{league_id}/...
```

NBA.com stable anchors:

- `LeagueID` identifies a Summer League venue/competition:
  - `15` = Las Vegas Summer League
  - `13` = California Classic
  - `16` = Salt Lake City Summer League
  - `14` = Orlando Pro Summer League
- `GAME_ID` is the stable game key.
- `PLAYER_ID` / `PERSON_ID` is the stable NBA.com person key.

## Architecture Principles

- Raw files are immutable source artifacts.
- Database audit rows describe raw files; they do not replace them.
- Normalization consumes audited raw files, not live NBA.com.
- Every normalized fact keeps raw NBA.com identity alongside canonical
  DraftGuru identity.
- Canonical player links are nullable and revisable.
- Unresolved player identity must never block stat ingestion.
- Derived/composite stats should be built later from normalized facts, not from
  raw JSON.

## Data Model

The summaries below explain the table intent. Schema implementation tickets
should treat the **Detailed Schema Contract** later in this section as binding:
it specifies expected column names, types, nullability, enum values, indexes,
and uniqueness constraints.

### Raw Audit Tables

#### `summer_league_raw_runs`

One row per raw scrape manifest.

Suggested fields:

- `id`
- `year`
- `league_id`
- `venue_slug`
- `started_at`
- `finished_at`
- `status`
- `team_gamelog_rows`
- `player_gamelog_rows`
- `game_count`
- `error_count`
- `manifest_path`
- `created_at`
- `updated_at`

Suggested constraints/indexes:

- unique: `(year, league_id, manifest_path)`
- index: `(year, league_id)`
- index: `status`

#### `summer_league_raw_files`

One row per raw JSON snapshot file.

Suggested fields:

- `id`
- `raw_run_id`
- `year`
- `league_id`
- `endpoint`
- `game_id` nullable
- `relative_path`
- `s3_key` nullable
- `sha256`
- `row_count`
- `parse_status`
- `parse_error` nullable
- `fetched_at` nullable
- `audited_at`

Suggested constraints/indexes:

- unique: `(raw_run_id, endpoint, game_id)`
- unique: `relative_path`
- index: `(year, league_id, endpoint)`
- index: `(game_id)`
- index: `parse_status`

### Normalized Product Tables

#### `summer_league_competitions`

One row per `(year, league_id)`.

Suggested fields:

- `id`
- `year`
- `league_id`
- `venue_slug`
- `display_name`
- `starts_on` nullable
- `ends_on` nullable
- `data_quality`: `full`, `partial`, `box_only`, `raw_only`
- `pbp_available`
- `shotchart_available`
- `raw_run_id`
- `created_at`
- `updated_at`

Suggested constraints/indexes:

- unique: `(year, league_id)`
- index: `(year, venue_slug)`
- FK: `raw_run_id -> summer_league_raw_runs.id`

#### `summer_league_team_entries`

One row per NBA team entry in one Summer League competition.

Suggested fields:

- `id`
- `competition_id`
- `nba_team_id` nullable FK to `nba_teams.id`
- `nba_stats_team_id`
- `raw_team_name`
- `raw_team_abbreviation`
- `team_slug`
- `wins` nullable
- `losses` nullable
- `created_at`
- `updated_at`

Suggested constraints/indexes:

- unique: `(competition_id, nba_stats_team_id)`
- index: `nba_team_id`
- index: `team_slug`

#### `summer_league_games`

One row per NBA.com `GAME_ID`.

Suggested fields:

- `id`
- `competition_id`
- `nba_stats_game_id`
- `game_date` nullable
- `home_team_entry_id` nullable
- `away_team_entry_id` nullable
- `home_score` nullable
- `away_score` nullable
- `status`
- `source_quality`
- `created_at`
- `updated_at`

Suggested constraints/indexes:

- unique: `nba_stats_game_id`
- index: `(competition_id, game_date)`
- FK: `competition_id -> summer_league_competitions.id`
- FK: `home_team_entry_id -> summer_league_team_entries.id`
- FK: `away_team_entry_id -> summer_league_team_entries.id`

#### `summer_league_source_players`

One row per NBA.com source identity. This table represents players before,
during, and after canonical DraftGuru resolution.

Suggested fields:

- `id`
- `nba_stats_person_id`
- `raw_player_name`
- `normalized_name`
- `first_seen_year`
- `last_seen_year`
- `canonical_player_id` nullable FK to `players_master.id`
- `resolution_status`: `UNRESOLVED`, `EXTERNAL_ID`, `EXACT`, `ALIAS`,
  `FUZZY`, `VECTOR_CANDIDATE`, `MANUAL`, `STUB`
- `resolution_confidence` nullable
- `resolution_candidates` JSONB nullable
- `resolved_at` nullable
- `resolved_by` nullable
- `created_at`
- `updated_at`

Suggested constraints/indexes:

- unique: `nba_stats_person_id`
- index: `canonical_player_id`
- index: `normalized_name`
- index: `resolution_status`

When a source player is resolved, the system should also ensure:

- `player_external_ids(system="nba_stats", external_id=<PERSON_ID>)` exists.
- Useful source spellings are optionally stored in `player_aliases`.

#### `summer_league_team_game_logs`

One row per team box-score line in one game.

Suggested fields:

- `id`
- `competition_id`
- `game_id`
- `team_entry_id`
- parsed team box totals
- parsed advanced team stats where available
- `source_endpoint`
- `created_at`
- `updated_at`

Suggested constraints/indexes:

- unique: `(game_id, team_entry_id)`
- index: `(competition_id, team_entry_id)`
- FK: `game_id -> summer_league_games.id`
- FK: `team_entry_id -> summer_league_team_entries.id`

#### `summer_league_player_game_logs`

One row per player box-score line in one game.

Suggested fields:

- `id`
- `competition_id`
- `game_id`
- `team_entry_id`
- `source_player_id`
- `player_id` nullable FK to `players_master.id`
- `nba_stats_person_id`
- `raw_player_name`
- `starter_position` nullable
- `minutes_seconds` nullable
- box columns: `pts`, `fgm`, `fga`, `fg3m`, `fg3a`, `ftm`, `fta`, `oreb`,
  `dreb`, `reb`, `ast`, `stl`, `blk`, `tov`, `pf`, `plus_minus`
- advanced columns where available: `off_rating`, `def_rating`, `net_rating`,
  `ast_pct`, `oreb_pct`, `dreb_pct`, `reb_pct`, `tm_tov_pct`, `efg_pct`,
  `ts_pct`, `usg_pct`, `pace`, `pie`
- scoring columns where available
- `source_endpoint`
- `created_at`
- `updated_at`

Suggested constraints/indexes:

- unique: `(game_id, nba_stats_person_id, team_entry_id)`
- index: `(competition_id, player_id)`
- index: `(competition_id, source_player_id)`
- index: `(team_entry_id)`
- FK: `game_id -> summer_league_games.id`
- FK: `source_player_id -> summer_league_source_players.id`
- FK: `player_id -> players_master.id`

`player_id` intentionally duplicates the canonical link from
`summer_league_source_players` for read performance and historical
traceability. A resolver backfill should be able to update this field after a
source player is resolved.

### Future Event Tables

These are out of initial scope, but the schema should not preclude them.

#### `summer_league_play_by_play_events`

Future row per NBA.com play-by-play event.

Key fields:

- `game_id`
- `event_num`
- `period`
- `clock`
- `event_type`
- `action_type`
- `team_entry_id`
- `source_player1_id`
- `source_player2_id`
- `source_player3_id`
- `score`
- `score_margin`
- `raw_payload` JSONB

Suggested unique key: `(game_id, event_num)`.

#### `summer_league_shots`

Future row per shot-chart event.

Key fields:

- `game_id`
- `source_player_id`
- `team_entry_id`
- `period`
- `minutes_remaining`
- `seconds_remaining`
- `shot_made`
- `shot_type`
- `shot_zone_basic`
- `shot_zone_area`
- `shot_zone_range`
- `loc_x`
- `loc_y`
- `shot_distance`
- `raw_payload` JSONB

### Detailed Schema Contract

Schema tickets should implement the tables below closely. If an implementing
agent discovers a source payload field name requires a different nullable shape,
they should document the reason in the PR and keep the public column contract as
stable as possible.

Use Python enums persisted with SQLAlchemy `Enum` columns where practical.
String values below are the canonical enum values.

#### Enum Contracts

| Enum | Values | Notes |
|---|---|---|
| `SummerLeagueRawRunStatus` | `PENDING`, `COMPLETE`, `PARTIAL`, `FAILED` | Status of auditing one raw manifest/run. |
| `SummerLeagueRawFileStatus` | `PRESENT`, `MISSING`, `EMPTY`, `PARSED`, `PARSE_FAILED`, `SKIPPED` | File-level audit/parse status. |
| `SummerLeagueDataQuality` | `full`, `partial`, `box_only`, `raw_only` | User-facing competition data quality. Lowercase values match existing planning docs. |
| `SummerLeagueGameStatus` | `scheduled`, `final`, `unknown` | Normalized game status; historical rows will usually be `final`. |
| `SummerLeagueResolutionStatus` | `UNRESOLVED`, `EXTERNAL_ID`, `EXACT`, `ALIAS`, `FUZZY`, `VECTOR_CANDIDATE`, `MANUAL`, `STUB` | Source-player to canonical-player resolution state. |
| `SummerLeagueReviewStatus` | `PENDING`, `APPROVED`, `REJECTED`, `STUB_CREATED` | Optional review queue lifecycle. |

#### `summer_league_raw_runs`

| Column | Type | Nullable | Default | Notes |
|---|---|---:|---|---|
| `id` | integer PK | no | identity | Primary key. |
| `year` | integer | no | none | Four-digit Summer League year. |
| `league_id` | string | no | none | NBA.com LeagueID, e.g. `15`. Store as string. |
| `venue_slug` | string | no | none | One of `las_vegas`, `orlando`, `california_classic`, `salt_lake_city`. |
| `status` | enum `SummerLeagueRawRunStatus` | no | `PENDING` | Derived from manifest/file audit outcome. |
| `started_at` | datetime | yes | none | From raw manifest. |
| `finished_at` | datetime | yes | none | From raw manifest. |
| `team_gamelog_rows` | integer | no | `0` | Manifest team row count. |
| `player_gamelog_rows` | integer | no | `0` | Manifest player row count. |
| `game_count` | integer | no | `0` | Manifest game count. |
| `error_count` | integer | no | `0` | `len(manifest.errors)`. |
| `manifest_path` | string | no | none | Local/S3-relative manifest path. |
| `manifest_sha256` | string | yes | none | Checksum of manifest JSON. |
| `s3_manifest_key` | string | yes | none | Durable archive key when uploaded. |
| `created_at` | datetime | no | `datetime.utcnow` | App timestamp. |
| `updated_at` | datetime | no | `datetime.utcnow` | App timestamp; update on upsert. |

Constraints and indexes:

- unique: `(year, league_id, manifest_path)`
- index: `(year, league_id)`
- index: `status`
- check: `year >= 2000`

#### `summer_league_raw_files`

| Column | Type | Nullable | Default | Notes |
|---|---|---:|---|---|
| `id` | integer PK | no | identity | Primary key. |
| `raw_run_id` | integer FK | no | none | FK to `summer_league_raw_runs.id`, cascade delete acceptable. |
| `year` | integer | no | none | Denormalized for filtering/reporting. |
| `league_id` | string | no | none | Denormalized NBA.com LeagueID. |
| `endpoint` | string | no | none | `manifest`, `leaguegamelog_team`, `leaguegamelog_player`, `boxscoretraditionalv2`, etc. |
| `game_id` | string | yes | none | NBA.com `GAME_ID` for game-level files; null for season-level files. |
| `relative_path` | string | no | none | Path relative to raw root. |
| `s3_key` | string | yes | none | Durable archive key. |
| `sha256` | string | yes | none | Null only when file is missing. |
| `byte_size` | integer | yes | none | Raw file size in bytes. |
| `row_count` | integer | yes | none | Primary result-set row count when parseable. |
| `parse_status` | enum `SummerLeagueRawFileStatus` | no | `PRESENT` | File audit/parse status. |
| `parse_error` | text | yes | none | Short parse failure detail. |
| `fetched_at` | datetime | yes | none | Best effort from manifest/run. |
| `audited_at` | datetime | no | `datetime.utcnow` | Last audit time. |
| `created_at` | datetime | no | `datetime.utcnow` | App timestamp. |
| `updated_at` | datetime | no | `datetime.utcnow` | App timestamp; update on upsert. |

Constraints and indexes:

- unique: `(raw_run_id, endpoint, game_id)`
- unique: `relative_path`
- index: `(year, league_id, endpoint)`
- index: `game_id`
- index: `parse_status`
- FK: `raw_run_id -> summer_league_raw_runs.id`

#### `summer_league_competitions`

| Column | Type | Nullable | Default | Notes |
|---|---|---:|---|---|
| `id` | integer PK | no | identity | Primary key. |
| `year` | integer | no | none | Four-digit Summer League year. |
| `league_id` | string | no | none | NBA.com LeagueID. |
| `venue_slug` | string | no | none | Venue slug. |
| `display_name` | string | no | none | Example: `2024 Las Vegas Summer League`. |
| `starts_on` | date | yes | none | Derived from source games when available. |
| `ends_on` | date | yes | none | Derived from source games when available. |
| `data_quality` | enum `SummerLeagueDataQuality` | no | `raw_only` | Updated by audit/normalization. |
| `pbp_available` | boolean | no | `False` | True when usable PBP exists for this competition. |
| `shotchart_available` | boolean | no | `False` | True when usable shot chart exists. |
| `raw_run_id` | integer FK | yes | none | FK to the audit run used for normalization. |
| `created_at` | datetime | no | `datetime.utcnow` | App timestamp. |
| `updated_at` | datetime | no | `datetime.utcnow` | App timestamp; update on upsert. |

Constraints and indexes:

- unique: `(year, league_id)`
- index: `(year, venue_slug)`
- FK: `raw_run_id -> summer_league_raw_runs.id`
- check: `year >= 2000`

#### `summer_league_team_entries`

| Column | Type | Nullable | Default | Notes |
|---|---|---:|---|---|
| `id` | integer PK | no | identity | Primary key. |
| `competition_id` | integer FK | no | none | FK to `summer_league_competitions.id`. |
| `nba_team_id` | integer FK | yes | none | FK to `nba_teams.id`; nullable until mapped. |
| `nba_stats_team_id` | string | no | none | Source `TEAM_ID`. Store as string to avoid integer-width surprises. |
| `raw_team_name` | string | no | none | Source team name. |
| `raw_team_abbreviation` | string | yes | none | Source abbreviation if present. |
| `team_slug` | string | no | none | URL-safe local slug derived from raw or canonical team. |
| `wins` | integer | yes | none | Optional competition record. |
| `losses` | integer | yes | none | Optional competition record. |
| `created_at` | datetime | no | `datetime.utcnow` | App timestamp. |
| `updated_at` | datetime | no | `datetime.utcnow` | App timestamp; update on upsert. |

Constraints and indexes:

- unique: `(competition_id, nba_stats_team_id)`
- index: `nba_team_id`
- index: `(competition_id, team_slug)`
- FK: `competition_id -> summer_league_competitions.id`
- FK: `nba_team_id -> nba_teams.id`

#### `summer_league_games`

| Column | Type | Nullable | Default | Notes |
|---|---|---:|---|---|
| `id` | integer PK | no | identity | Primary key. |
| `competition_id` | integer FK | no | none | FK to `summer_league_competitions.id`. |
| `nba_stats_game_id` | string | no | none | Stable NBA.com `GAME_ID`. |
| `game_date` | date | yes | none | Parsed from gamelog if present. |
| `home_team_entry_id` | integer FK | yes | none | Source home team if known. |
| `away_team_entry_id` | integer FK | yes | none | Source away team if known. |
| `home_score` | integer | yes | none | Final score if known. |
| `away_score` | integer | yes | none | Final score if known. |
| `status` | enum `SummerLeagueGameStatus` | no | `unknown` | Usually `final` after normalization. |
| `source_quality` | enum `SummerLeagueDataQuality` | no | `raw_only` | Game-level quality. |
| `created_at` | datetime | no | `datetime.utcnow` | App timestamp. |
| `updated_at` | datetime | no | `datetime.utcnow` | App timestamp; update on upsert. |

Constraints and indexes:

- unique: `nba_stats_game_id`
- index: `(competition_id, game_date)`
- index: `home_team_entry_id`
- index: `away_team_entry_id`
- FK: `competition_id -> summer_league_competitions.id`
- FK: `home_team_entry_id -> summer_league_team_entries.id`
- FK: `away_team_entry_id -> summer_league_team_entries.id`

#### `summer_league_source_players`

| Column | Type | Nullable | Default | Notes |
|---|---|---:|---|---|
| `id` | integer PK | no | identity | Primary key. |
| `nba_stats_person_id` | string | no | none | NBA.com `PLAYER_ID` / `PERSON_ID`. |
| `raw_player_name` | string | no | none | Best/current source spelling. |
| `normalized_name` | string | no | none | Name key from existing normalization helper. |
| `first_seen_year` | integer | yes | none | Earliest ingested Summer League year for this source player. |
| `last_seen_year` | integer | yes | none | Latest ingested Summer League year for this source player. |
| `canonical_player_id` | integer FK | yes | none | FK to `players_master.id`; nullable until resolved. |
| `resolution_status` | enum `SummerLeagueResolutionStatus` | no | `UNRESOLVED` | Current resolution state. |
| `resolution_confidence` | float | yes | none | Optional 0.0-1.0 score for fuzzy/vector paths. |
| `resolution_candidates` | JSONB | yes | none | Candidate list: `[{player_id, display_name, score, method}]`. |
| `resolved_at` | datetime | yes | none | Timestamp for current resolution. |
| `resolved_by` | string | yes | none | `system`, admin email, or operator identifier. |
| `created_at` | datetime | no | `datetime.utcnow` | App timestamp. |
| `updated_at` | datetime | no | `datetime.utcnow` | App timestamp; update on upsert. |

Constraints and indexes:

- unique: `nba_stats_person_id`
- index: `canonical_player_id`
- index: `normalized_name`
- index: `resolution_status`
- FK: `canonical_player_id -> players_master.id`
- check: `resolution_confidence IS NULL OR resolution_confidence BETWEEN 0 AND 1`

#### `summer_league_team_game_logs`

| Column | Type | Nullable | Default | Notes |
|---|---|---:|---|---|
| `id` | integer PK | no | identity | Primary key. |
| `competition_id` | integer FK | no | none | FK to competition. |
| `game_id` | integer FK | no | none | FK to normalized game. |
| `team_entry_id` | integer FK | no | none | FK to competition team. |
| `minutes` | integer | yes | none | Team minutes if present. |
| `pts` | integer | yes | none | Points. |
| `fgm` | integer | yes | none | Field goals made. |
| `fga` | integer | yes | none | Field goals attempted. |
| `fg_pct` | float | yes | none | Source FG%. |
| `fg3m` | integer | yes | none | Three-pointers made. |
| `fg3a` | integer | yes | none | Three-pointers attempted. |
| `fg3_pct` | float | yes | none | Source 3P%. |
| `ftm` | integer | yes | none | Free throws made. |
| `fta` | integer | yes | none | Free throws attempted. |
| `ft_pct` | float | yes | none | Source FT%. |
| `oreb` | integer | yes | none | Offensive rebounds. |
| `dreb` | integer | yes | none | Defensive rebounds. |
| `reb` | integer | yes | none | Total rebounds. |
| `ast` | integer | yes | none | Assists. |
| `stl` | integer | yes | none | Steals. |
| `blk` | integer | yes | none | Blocks. |
| `tov` | integer | yes | none | Turnovers. |
| `pf` | integer | yes | none | Personal fouls. |
| `plus_minus` | integer | yes | none | Plus/minus if source provides it. |
| `off_rating` | float | yes | none | Advanced endpoint. |
| `def_rating` | float | yes | none | Advanced endpoint. |
| `net_rating` | float | yes | none | Advanced endpoint. |
| `ast_pct` | float | yes | none | Advanced endpoint. |
| `reb_pct` | float | yes | none | Advanced endpoint. |
| `efg_pct` | float | yes | none | Advanced endpoint. |
| `ts_pct` | float | yes | none | Advanced endpoint. |
| `pace` | float | yes | none | Advanced endpoint. |
| `source_endpoint` | string | no | `boxscoretraditionalv2` | Primary endpoint used for the row. |
| `created_at` | datetime | no | `datetime.utcnow` | App timestamp. |
| `updated_at` | datetime | no | `datetime.utcnow` | App timestamp; update on upsert. |

Constraints and indexes:

- unique: `(game_id, team_entry_id)`
- index: `(competition_id, team_entry_id)`
- FK: `competition_id -> summer_league_competitions.id`
- FK: `game_id -> summer_league_games.id`
- FK: `team_entry_id -> summer_league_team_entries.id`

#### `summer_league_player_game_logs`

| Column | Type | Nullable | Default | Notes |
|---|---|---:|---|---|
| `id` | integer PK | no | identity | Primary key. |
| `competition_id` | integer FK | no | none | FK to competition. |
| `game_id` | integer FK | no | none | FK to normalized game. |
| `team_entry_id` | integer FK | no | none | FK to competition team. |
| `source_player_id` | integer FK | no | none | FK to NBA.com source player identity. |
| `player_id` | integer FK | yes | none | Denormalized canonical player FK; nullable until resolved. |
| `nba_stats_person_id` | string | no | none | Source `PLAYER_ID`/`PERSON_ID`. |
| `raw_player_name` | string | no | none | Source row spelling. |
| `starter_position` | string | yes | none | Source `START_POSITION`, if present. |
| `comment` | string | yes | none | Source comment/status for DNP rows if present. |
| `minutes_seconds` | integer | yes | none | Total seconds parsed from source minutes string. |
| `pts` | integer | yes | none | Points. |
| `fgm` | integer | yes | none | Field goals made. |
| `fga` | integer | yes | none | Field goals attempted. |
| `fg_pct` | float | yes | none | Source FG%. |
| `fg3m` | integer | yes | none | Three-pointers made. |
| `fg3a` | integer | yes | none | Three-pointers attempted. |
| `fg3_pct` | float | yes | none | Source 3P%. |
| `ftm` | integer | yes | none | Free throws made. |
| `fta` | integer | yes | none | Free throws attempted. |
| `ft_pct` | float | yes | none | Source FT%. |
| `oreb` | integer | yes | none | Offensive rebounds. |
| `dreb` | integer | yes | none | Defensive rebounds. |
| `reb` | integer | yes | none | Total rebounds. |
| `ast` | integer | yes | none | Assists. |
| `stl` | integer | yes | none | Steals. |
| `blk` | integer | yes | none | Blocks. |
| `tov` | integer | yes | none | Turnovers. |
| `pf` | integer | yes | none | Personal fouls. |
| `plus_minus` | integer | yes | none | Plus/minus. |
| `off_rating` | float | yes | none | Advanced endpoint. |
| `def_rating` | float | yes | none | Advanced endpoint. |
| `net_rating` | float | yes | none | Advanced endpoint. |
| `ast_pct` | float | yes | none | Advanced endpoint. |
| `oreb_pct` | float | yes | none | Advanced endpoint. |
| `dreb_pct` | float | yes | none | Advanced endpoint. |
| `reb_pct` | float | yes | none | Advanced endpoint. |
| `tm_tov_pct` | float | yes | none | Advanced endpoint. |
| `efg_pct` | float | yes | none | Advanced endpoint. |
| `ts_pct` | float | yes | none | Advanced endpoint. |
| `usg_pct` | float | yes | none | Advanced endpoint. |
| `pace` | float | yes | none | Advanced endpoint. |
| `pie` | float | yes | none | Advanced endpoint. |
| `pct_fga_2pt` | float | yes | none | Scoring endpoint, if present. |
| `pct_fga_3pt` | float | yes | none | Scoring endpoint, if present. |
| `pct_pts_2pt` | float | yes | none | Scoring endpoint, if present. |
| `pct_pts_3pt` | float | yes | none | Scoring endpoint, if present. |
| `pct_pts_ft` | float | yes | none | Scoring endpoint, if present. |
| `source_endpoint` | string | no | `boxscoretraditionalv2` | Primary endpoint used for the row. |
| `created_at` | datetime | no | `datetime.utcnow` | App timestamp. |
| `updated_at` | datetime | no | `datetime.utcnow` | App timestamp; update on upsert. |

Constraints and indexes:

- unique: `(game_id, nba_stats_person_id, team_entry_id)`
- index: `(competition_id, player_id)`
- index: `(competition_id, source_player_id)`
- index: `team_entry_id`
- index: `nba_stats_person_id`
- FK: `competition_id -> summer_league_competitions.id`
- FK: `game_id -> summer_league_games.id`
- FK: `team_entry_id -> summer_league_team_entries.id`
- FK: `source_player_id -> summer_league_source_players.id`
- FK: `player_id -> players_master.id`

#### `summer_league_player_resolution_reviews`

This table is optional in the first milestone if the project elects CLI-only
review, but if ticketed it should use this contract.

| Column | Type | Nullable | Default | Notes |
|---|---|---:|---|---|
| `id` | integer PK | no | identity | Primary key. |
| `source_player_id` | integer FK | no | none | FK to `summer_league_source_players.id`. |
| `raw_player_name` | string | no | none | Snapshot of source spelling at review creation. |
| `nba_stats_person_id` | string | no | none | Snapshot of source person ID. |
| `candidate_players` | JSONB | yes | none | Candidate list: `[{player_id, display_name, score, method}]`. |
| `status` | enum `SummerLeagueReviewStatus` | no | `PENDING` | Review lifecycle. |
| `selected_player_id` | integer FK | yes | none | Selected canonical player, if any. |
| `review_note` | text | yes | none | Human/operator note. |
| `created_at` | datetime | no | `datetime.utcnow` | App timestamp. |
| `reviewed_at` | datetime | yes | none | Review completion timestamp. |

Constraints and indexes:

- unique: `(source_player_id, status)` for active `PENDING` rows if implemented
  as a partial unique index; otherwise enforce one pending review in service code.
- index: `status`
- index: `selected_player_id`
- FK: `source_player_id -> summer_league_source_players.id`
- FK: `selected_player_id -> players_master.id`

## Player Resolution Strategy

Summer League player normalization should mirror the board-ingestion pattern
while using NBA.com `PERSON_ID` as the strongest source signal.

Resolution order:

1. **External ID**
   - If `player_external_ids(system="nba_stats", external_id=PERSON_ID)`
     exists, attach immediately.

2. **Existing Source Identity**
   - If `summer_league_source_players.nba_stats_person_id` already has
     `canonical_player_id`, reuse it.

3. **Exact Normalized Name**
   - Match `players_master.display_name` using the existing suffix/diacritic
     normalization pattern.

4. **Alias**
   - Match `player_aliases.full_name`.

5. **Contextual Fuzzy Match**
   - Use high-threshold name similarity with context such as draft year near
     Summer League year, existing prospect status, school, and known draft
     facts when available.
   - Auto-resolve only at very high confidence.

6. **Hybrid Candidate Search**
   - Use `app.services.player_search_service.find_candidate_players()` to
     collect lexical/vector candidates.
   - Store candidates on `summer_league_source_players.resolution_candidates`
     or a review table.

7. **Stub Creation**
   - If no serious candidate exists, create a `PlayerMaster` stub:
     - `display_name = raw_player_name`
     - `is_stub = True`
     - `bio_source = "summer_league_ingest"`
   - Write `player_external_ids(system="nba_stats", external_id=PERSON_ID)`.

8. **Review**
   - Ambiguous candidates should remain unresolved and reviewable.
   - Unresolved rows can still power Summer League pages under raw NBA.com
     source identity.

## Pipeline Commands

Expected command surfaces:

### Archive Raw Files

```bash
conda run -n draftguru python scripts/archive_summer_league_raw.py \
  --raw-root data/raw/nba_stats/summer_league \
  --s3-prefix s3://<bucket>/raw/nba_stats/summer_league \
  --dry-run
```

### Audit Raw Files

```bash
conda run -n draftguru python scripts/audit_summer_league_raw.py \
  --raw-root data/raw/nba_stats/summer_league
```

### Normalize From Audit

```bash
conda run -n draftguru python scripts/normalize_summer_league.py \
  --year 2024 \
  --league-id 15
```

### Resolve Source Players

```bash
conda run -n draftguru python scripts/resolve_summer_league_players.py \
  --year 2024 \
  --league-id 15
```

### End-to-End Backfill

```bash
conda run -n draftguru python scripts/backfill_summer_league_backbone.py \
  --year 2024 \
  --league-id 15
```

Recommended options across commands:

- `--year`
- `--league-id`
- `--raw-root`
- `--s3-prefix`
- `--dry-run`
- `--force`
- `--limit-games`
- `--report-path`

## End-to-End QA Harness

Add a dedicated QA harness:

```bash
conda run -n draftguru python scripts/qa_summer_league_backbone.py \
  --year 2024 \
  --league-id 15 \
  --raw-root data/raw/nba_stats/summer_league
```

The harness should emit a human-readable report, recommended path:

```text
docs/qa/summer-league-backbone-qa-YYYY-MM-DD.md
```

### QA Stress Slices

The final QA gate should run against multiple data shapes:

- `2024/15` or `2025/15`: modern full Vegas path.
- `2024/13`: modern satellite venue.
- `2010/14`: older Orlando partial/weird endpoint path.
- `2007/15`: earliest Vegas scrape path.

The goal is not identical coverage across eras. The goal is accurate
classification, stable normalization, and honest partial-data reporting.

### QA Acceptance Criteria

#### Raw Archive Integrity

- Every expected raw file is archived or explicitly reported as missing.
- Every raw file has a checksum.
- Re-running archive/audit does not create duplicate audit rows.
- Manifest `game_count`, `team_gamelog_rows`, and `player_gamelog_rows` are
  preserved in `summer_league_raw_runs`.

#### Audit Completeness

- Every manifest-listed game has audited endpoint records.
- Endpoint coverage is classified by competition.
- Missing or empty endpoints are visible through `parse_status`, `parse_error`,
  or competition quality flags.
- Audit reports include row counts and endpoint availability.

#### Normalization Parity

- `summer_league_competitions` has one row per audited `(year, league_id)`.
- `summer_league_games` count equals manifest `game_count`.
- `summer_league_team_entries` matches distinct source team IDs/names.
- `summer_league_team_game_logs` count matches parsed team box rows.
- `summer_league_player_game_logs` count matches parsed player box rows.
- Every normalized game log points to valid competition/game/team/source-player
  rows.

#### Idempotency

- Run the full backfill once.
- Snapshot row counts and key checksums.
- Run it again.
- Expected result: no duplicate rows, no changed counts, and no changed
  canonical mappings unless forced.

#### Player Resolution

- Known NBA.com `PERSON_ID` values resolve through `player_external_ids`.
- Exact normalized names resolve.
- Alias variants resolve.
- Ambiguous names do not auto-resolve.
- Unresolved source players still ingest successfully.
- Stub creation writes both `players_master` and `player_external_ids`.
- Updating a source player's canonical resolution can backfill `player_id` onto
  existing game logs without rewriting raw facts.

#### Historical Edge Cases

- Modern full competitions classify as `full` when all expected endpoints are
  present.
- Older partial competitions classify as `partial` or `box_only`.
- Missing PBP or shot-chart data does not block box-score normalization.
- Raw endpoint weirdness is visible in QA output.

#### Referential Integrity

- No orphaned `summer_league_player_game_logs`.
- No orphaned `summer_league_team_game_logs`.
- No duplicate `nba_stats_game_id`.
- No duplicate source player for one `PERSON_ID`.
- No game logs with missing `source_player_id`.
- Nullable `player_id` appears only for unresolved/review-needed source
  players.

#### Failure and Re-run Behavior

- A corrupt raw file marks parse failure without killing the entire competition.
- A missing endpoint marks partial quality.
- Dry-run reports intended writes without DB mutation.
- Force mode updates parsed rows deterministically.

## Required Tests

Follow repo conventions from `docs/plans/ai-orchestrator-ticket-spec.md`.

Minimum test coverage:

- Unit tests for manifest parsing, endpoint row counting, path/key derivation,
  checksum generation, and player-name normalization.
- Integration tests for raw audit schema and audit upserts.
- Integration tests for normalized schema constraints and idempotent upserts.
- Integration tests for competition/team/game normalization.
- Integration tests for source-player and player-game-log normalization.
- Unit/integration tests for resolution cascade behavior.
- Integration tests for end-to-end backfill idempotency against a small fixture
  raw tree.
- QA harness tests proving it catches count mismatches, orphaned rows, duplicate
  rows, missing endpoints, and unresolved-player states.

Required repo checks after implementation work:

```bash
conda run -n draftguru make precommit
conda run -n draftguru mypy app --ignore-missing-imports
conda run -n draftguru pytest tests/unit -q
conda run -n draftguru make coverage.diff TESTS=tests/unit
```

Run relevant integration tests for DB-touching tickets.

## Ticket Breakdown

### 1. Write Summer League Data Backbone Spec

Produce and maintain this project spec plus companion QA/test-plan artifacts if
the project is converted into GitHub issues.

Primary files:

- `docs/plans/summer-league-data-backbone.md`
- `docs/plans/summer-league-data-backbone-qa-checklist.md`
- `docs/plans/summer-league-data-backbone-test-plan.md`

### 2. Archive Raw Summer League Snapshots to S3

Upload existing raw scrape files to durable storage while preserving relative
paths. Add a dry-run/reporting script.

Depends on: ticket 1.

### 3. Add Raw Audit Schema

Add `summer_league_raw_runs` and `summer_league_raw_files`, including Alembic
migration and integration schema import.

Depends on: ticket 1.

### 4. Build Raw Audit Scanner

Read local/S3 raw files, parse manifests, compute checksums/row counts, and
upsert raw audit metadata. Emit a coverage report.

Depends on: tickets 2 and 3.

### 5. Add Summer League Product Schema

Add normalized product tables for competitions, team entries, games, source
players, team game logs, and player game logs.

Depends on: ticket 1. Should be sequenced after ticket 3 if migrations overlap.

### 6. Build Competition/Game/Team Normalizer

Normalize audited raw files into competitions, team entries, games, and team
game logs. Do not perform player resolution in this ticket.

Depends on: tickets 4 and 5.

### 7. Build Source Player and Player Game Log Normalizer

Populate `summer_league_source_players` and `summer_league_player_game_logs`.
Store raw source identity and nullable canonical `player_id`.

Depends on: ticket 6.

### 8. Build Summer League Player Resolution Service

Implement the resolution cascade using external IDs, exact names, aliases,
contextual fuzzy matching, hybrid lexical/vector candidates, stubs, and
review-needed states.

Depends on: ticket 7.

### 9. Add Player Resolution Review Queue

Add optional review persistence for ambiguous source players. This can be
deferred if the project elects to start with CLI-only review.

Depends on: ticket 8.

### 10. Create End-to-End Backfill Command

Create one command that can run audit, normalization, source-player ingestion,
and player resolution for selected years/LeagueIDs.

Depends on: tickets 4, 6, 7, and 8.

### 11. Build Summer League QA Validation Service

Create reusable validation helpers under `app/services/summer_league/qa.py`.
These helpers should return structured findings for raw archive integrity,
audit completeness, normalization parity, idempotency snapshots, player
resolution, historical edge cases, referential integrity, and failure behavior.

Depends on: ticket 10.

Recommended agent: `gpt-5.4`.

### 12. Build Summer League QA CLI and Report Writer

Create `scripts/qa_summer_league_backbone.py` as a thin CLI over the validation
service. It should support stress-slice selection and write a Markdown report
under `docs/qa/`.

Depends on: ticket 11.

Recommended agent: `gpt-5.4`.

### 13. Add QA Stress Fixtures and Negative Cases

Add small raw-fixture trees and tests proving the QA service/CLI catches count
mismatches, orphaned rows, duplicate rows, missing endpoints, corrupt raw files,
and unresolved-player states. This ticket should avoid using the full local raw
scrape as a test fixture.

Depends on: tickets 11 and 12.

Recommended agent: `gpt-5.4`.

### 14. Cross-System QA Gate

Run the full implementation checks and QA harness against the stress slices.
Produce a final QA report under `docs/qa/`. This is intentionally a larger
whole-system verification ticket and should use a stronger model.

Depends on: ticket 13.

Recommended agent: `gpt-5.5`.

## Dependency Graph

```text
Spec
├─ S3 Archive
├─ Raw Audit Schema ─ Raw Audit Scanner
└─ Product Schema ─ Competition/Game/Team Normalizer
                 └─ Source Player/Game Log Normalizer
                    └─ Player Resolution Service
                       └─ Review Queue
                          └─ End-to-End Backfill
                             └─ QA Validation Service
                                └─ QA CLI + Report Writer
                                   └─ QA Stress Fixtures
                                      └─ Cross-System QA Gate
```

## Completion Bar

The project is complete when QA can demonstrate:

1. Raw scrape files are durably archived or explicitly accounted for.
2. Audit metadata exactly describes the raw scrape inventory.
3. Normalized tables preserve source row counts and stable NBA.com IDs.
4. The backfill is idempotent.
5. Player resolution handles external-ID, exact, alias, ambiguous, unresolved,
   and stub cases.
6. Historical partial data is classified instead of hidden.
7. Referential integrity checks pass across all normalized tables.
8. The final QA report contains enough counts, examples, and failure summaries
   for another agent to diagnose the pipeline without manually reading raw JSON.
