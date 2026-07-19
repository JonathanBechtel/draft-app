# Summer League Explorer — League Context Technical Spec

> **Superseded for planning by** `docs/plans/event-environment-intelligence-pitch.md`.
> Retain its source-snapshot and NBA comparison details as an implementation reference
> for the optional comparison lens, not as the primary product architecture.

**Status:** Proposed implementation spec · **Date:** 2026-07-17

**Product inputs:**

- `docs/plans/summer-league-context-benchmarks-pitch.md`
- `docs/plans/summer-league-explorer-context-qa-checklist.md`
- `docs/plans/summer-league-explorer-context-test-plan.md`

## 1. Goal

Make league-level Summer League context available inside
`/stats/summer-league/explorer` as a shareable **League Context** subject. For one
selected Summer League competition, show a reproducible comparison against the
immediately preceding NBA regular season:

`/stats/summer-league/explorer?subject=context&year_min=2025&year_max=2025&venue=las_vegas`

The view shows at-rim FG%, 3P%, 3PA share, assisted-FG rate, TOV%, pace per 48, and
offensive rating. It must read a precomputed projection only; a public request must
never call NBA Stats.

## 2. Scope and non-goals

### In scope

- Explorer subject `context`, plus a compact linked preview in a pinned Teams query.
- One Summer League competition ↔ one NBA regular-season benchmark.
- Versioned source metadata, coverage gating, formulas, CSV export, and server-rendered
  URL round trips.
- Offline aggregation of existing Summer League normalized facts and NBA Stats source
  snapshots.

### Out of scope

- Player translation/projection models, NBA playoff comparators, or cross-venue pooling.
- A live NBA Stats dependency on public route reads.
- A new canonical player identity or parallel player-data store.

## 3. Data model

Add `app/schemas/league_context.py`, imported through Alembic’s existing schema
discovery path.

### 3.1 Source snapshot metadata

`league_context_source_snapshots`

| Field | Notes |
| --- | --- |
| `id` | primary key |
| `source_system` | initially `nba_stats` |
| `source_kind` | `team_game_log` or `team_shot_locations` |
| `league_id`, `season_key`, `season_type` | exact source scope; e.g. `15`, `2025`, `Regular Season` |
| `source_url`, `retrieved_at`, `content_sha256` | reproducibility and dedupe |
| `archive_uri` | immutable raw-response location; do not require large payload JSON in Postgres |
| `row_count`, `coverage` | source-level completeness metadata |

Source snapshots are observations/provenance. They are immutable; a refetch creates a
new snapshot rather than overwriting an old response.

### 3.2 Aggregate input projection

`league_context_aggregates`

One rebuildable aggregate for either a Summer League competition or NBA regular-season
scope. Its raw inputs are sufficient to recompute every display metric:

| Field group | Fields |
| --- | --- |
| scope | `id`, `scope_kind`, `competition_id` (nullable for NBA), `league_id`, `season_key`, `display_label` |
| coverage | `team_games`, `complete_games`, `has_complete_team_boxes`, `has_complete_zone_data`, `coverage_status` |
| box totals | `team_minutes`, `pts`, `fgm`, `fga`, `fg3m`, `fg3a`, `ftm`, `fta`, `oreb`, `dreb`, `ast`, `tov` |
| zone totals | `rim_fgm`, `rim_fga` |
| possession input | `estimated_possessions` (sum of opponent-adjusted team possessions) |
| provenance | `source_snapshot_ids` JSONB, `calculation_version`, `computed_at` |

Uniqueness:

- Summer League: one active aggregate per `(scope_kind, competition_id,
  calculation_version)`.
- NBA: one active aggregate per `(scope_kind, league_id, season_key, season_type,
  calculation_version)`.

Index the competition lookup and NBA season lookup. Use a partial active/current index
only if version retention makes it necessary after measuring query plans.

### 3.3 Explorer-ready benchmark projection

`summer_league_context_benchmarks`

One current, materialized projection per `(summer_league_competition_id,
nba_aggregate_id, calculation_version)`. It stores:

- stable links to both input aggregates;
- source/coverage state and an unavailable reason when inputs fail the gate;
- the seven Summer League values, NBA values, and signed differences;
- `calculation_version`, `computed_at`, and `is_current`.

Storing display values keeps the request path one indexed projection read and makes CSV
exports exactly reproducible. The values are replaceable outputs—not canonical facts.

## 4. Metric contract

All percentage values are stored/displayed on the 0–100 scale. Differences for percent
metrics are percentage points. Numeric pace/rating differences use the same scale as
their values.

```text
at_rim_fg_pct       = 100 * rim_fgm / rim_fga
three_fg_pct        = 100 * fg3m / fg3a
three_attempt_share = 100 * fg3a / fga
assisted_fg_rate    = 100 * ast / fgm
tov_pct             = 100 * tov / (fga + 0.44 * fta + tov)
offensive_rating    = 100 * pts / estimated_possessions
pace_per_48         = 48 * estimated_possessions / (team_minutes / 5)
```

`estimated_possessions` uses the project’s existing opponent-adjusted possession
formula in `app.services.summer_league.metrics.Box.poss`, including `0.4 × FTA` and the
offensive-rebound adjustment. Reuse/extract that implementation rather than creating a
second drifting formula.

Zone data uses NBA Stats `Restricted Area`; backcourt heaves remain excluded consistently
with `summer_league_shotchart_service`.

## 5. Offline ingestion and rebuild

### Summer League input

Aggregate from normalized `SummerLeagueTeamGameLog`, `SummerLeagueGame`, and
`SummerLeagueShotEvent` for one `SummerLeagueCompetition`. Include completed games only,
match the existing metric pipeline’s status policy, and gate on full team-box and zone
coverage. Do not recompute this data in the request path.

### NBA input

Add an offline NBA benchmark fetcher using the existing Chrome-TLS `NBAStatsClient`:

- `leaguegamelog` with team rows for raw box totals;
- `leaguedashteamshotlocations` with `DistanceRange=By Zone` for rim totals.

Persist raw responses in the existing raw archive convention, write immutable source
snapshot metadata, then aggregate. The first supported pairing is `LeagueID=00`, NBA
regular season `YYYY-YY`; the Summer League `YYYY` maps to NBA `YYYY-1–YY`.

### Orchestration

Add a CLI/rebuild entry point, conceptually:

```text
python -m app.cli.rebuild_summer_league_context --competition-id <id>
```

It may fetch a missing NBA snapshot only in the offline command. A normal run rebuilds
the current Summer League aggregate, validates coverage, pairs the NBA season, and
upserts the current benchmark projection transactionally. The existing Summer League
ingestion runner can invoke this after its metrics rebuild once a competition is complete
enough; early/incomplete events retain an explicit unavailable projection.

## 6. Explorer read path

### Query parsing

Extend `SUBJECTS` and `parse_query()` with `context`. `context` ignores player-grain,
mode, pagination, sort, and stat filters; normalize those irrelevant inputs rather than
letting them alter output. A valid query satisfies:

```text
year_min == year_max != None
venue != None
```

Resolve `(year, venue)` to exactly one `SummerLeagueCompetition`. No result or multiple
results yields a structured unavailable state, not an exception.

### DTO and service

Create `app/services/summer_league_context_service.py` with a small read DTO:

```text
LeagueContextView(
  state: ready | needs_scope | unavailable,
  competition_label, comparator_label,
  metrics[], source_links[], coverage_note, calculation_version
)
```

`run_explorer_query()` dispatches `context` to that service. Extend `ExplorerResult`
with an optional `context` payload instead of forcing a non-ranking metric matrix into
the normal rows/columns table.

### Templates and CSV

- Add the Context tab in `explorer.html`.
- Add a focused `_explorer_context.html` partial rendered in the normal result region.
- Show scope guidance for `needs_scope`, coverage/source reason for `unavailable`, and
  the comparison matrix plus definitions/source links for `ready`.
- Add a context-specific CSV serializer with scope identifiers, every value/difference,
  coverage, calculation version, and source references.
- In Teams results, ask the same read service for a preview only when the query is a
  valid single competition. The preview links to the equivalent `subject=context` URL.

All templates remain server-rendered; the existing Explorer AJAX swap enhances them but
is not required for a valid cold load.

## 7. API, performance, and migration notes

- No public JSON API is required in v1; HTML and CSV follow Explorer conventions.
- Public Context read should be one projection lookup plus, at most, a bounded source
  metadata lookup. The Teams preview must not cause an N+1 query per team.
- Add Alembic migration for three tables and their foreign keys/indexes. Do not alter or
  rebuild existing Summer League raw/stat tables.
- Run `make perf` for Context and Teams preview. Run `make explain` against a prod-like
  database for the context projection lookup; ship an index/migration for any missing
  access path.

## 8. Files expected to change

| Purpose | Path |
| --- | --- |
| schemas and enums | `app/schemas/league_context.py` |
| migration | `alembic/versions/<revision>_add_league_context_benchmarks.py` |
| offline fetch/rebuild | `app/services/league_context/` and `app/cli/rebuild_summer_league_context.py` |
| read DTO/service | `app/services/summer_league_context_service.py` |
| Explorer dispatcher/query parsing | `app/services/summer_league_explorer_service.py` |
| route/CSV dispatch | `app/routes/summer_league.py` |
| Explorer tabs/results/preview | `app/templates/stats/summer-league/explorer.html`, `_explorer_results.html`, `_explorer_context.html` |
| styles/interaction | `app/static/css/summer-league-games.css`, `app/static/summer-league-explorer.js` if needed |
| tests | `tests/unit/test_summer_league_context*.py`, `tests/integration/test_summer_league_explorer_context.py` |

## 9. Journey-graph alignment

The raw event facts remain in the existing Summer League stat spoke; NBA source responses
are provenance-backed observations for a separate NBA league-season input. The benchmark
is a versioned, rebuildable projection between two scopes. It introduces neither a
parallel player store nor mutable canonical facts, so it advances the hub-and-spoke,
assertion-and-projection backbone and gives later sports/leagues a reusable context
contract.

## 10. Acceptance criteria

1. A valid pinned Explorer Context URL renders the complete, sourced comparison without
   any live NBA request.
2. Any broad, invalid, incomplete, or unpaired scope renders a clear unavailable state
   and never blends venues or fabricates values.
3. All seven metrics recompute from persisted totals using the contract in §4.
4. Teams preview, shared URL, CSV, and JS-disabled cold load agree with the same
   projection.
5. Coverage/provenance/calculation version are persisted and visible enough to audit.
6. Migration, lint/type/test/coverage/perf/explain/visual checks meet the repository’s
   Definition of Done.
