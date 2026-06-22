# Summer League Explorer — QA Checklist

**Sources:**
- Feature spec: `docs/plans/summer-league-explorer-roadmap-spec.md`
- Orchestrator defaults: `docs/plans/ai-orchestrator-ticket-spec.md`

**Scope:** All four phases of the Explorer roadmap. Each section maps to one phase.
Integration tests are the primary signal; browser verification covers the form UX
and URL-shareable state.

---

## Phase 1 — Quick wins / bug fixes

### 1a. `draft_round` index

- The Alembic migration adds an index on `players_master.draft_round`.
  - Verify: `make explain ROUTE=/stats/summer-league/explorer?draft_round=1` against a
    prod-like DB.
  - Expected: query plan shows Index Scan on `players_master` using the new index, not a
    Seq Scan.
  - Evidence: `EXPLAIN ANALYZE` output in PR or QA notes.

- The index does not change query results.
  - Verify: integration test with `draft_round=1` and `draft_round=2` before and after
    migration.
  - Expected: identical result sets; no regressions in existing Explorer tests.

### 1b. New stat columns (OREB, DREB, PF, +/-, eFG%)

- OREB and DREB appear in the results table for all four modes.
  - Verify: integration test — seed game logs with known `oreb` / `dreb` values, query
    Explorer with default filters, assert OREB and DREB in response.
  - Expected: correct totals; per-game scales by GP; per-36 scales by minutes; per-100
    scales by possessions.

- PF appears in results table and scales correctly across modes.
  - Verify: same pattern as OREB/DREB.

- `plus_minus` appears per-game and in totals; is suppressed (or blank) in per-36 and
  per-100 modes.
  - Verify: integration test — assert column present in `per_game` and `totals` mode rows,
    absent or null in `per_36` and `per_100`.
  - Expected: no crash; graceful suppression.

- `eFG%` is correctly calculated: `(FGM + 0.5 × 3PM) / FGA × 100`.
  - Verify: unit test in `tests/unit/test_summer_league_explorer.py` with known values.
  - Expected: known input → known output; division-by-zero (FGA = 0) returns `—` in the
    template, not an error.

- All five new columns are sortable (headers link correctly and sort order toggles).
  - Verify: browser — load Explorer, click each new column header twice; confirm sort
    direction icon and row order change.
  - Expected: ascending and descending both work; active column is highlighted.

### 1c. Position filter

- Position dropdown populates from live data.
  - Verify: integration test — seed players with distinct positions; load Explorer; assert
    position select options include seeded values.
  - Expected: no empty dropdown when positions exist in DB.

- Filtering by a position returns only players at that position.
  - Verify: integration test — seed G and F players, query `?position=G`, assert only G
    rows returned.
  - Expected: F players absent from results.

- Position filter persists through sort and page navigation.
  - Verify: browser — set position filter, sort by PTS, page to page 2; confirm URL
    contains `position=G` throughout.
  - Expected: filter is not lost on re-sort or pagination.

- New `PlayerMaster.position` index confirmed via `make explain`.
  - Expected: Index Scan on `players_master` when position filter is active.

### 1d. Undrafted toggle

- `?undrafted=1` returns only players with `draft_year IS NULL`.
  - Verify: integration test — seed one drafted and one undrafted player with game logs;
    query with `undrafted=1`; assert only undrafted player returned.

- `undrafted=1` overrides / ignores `draft_class` and `draft_round` params.
  - Verify: integration test — query `?undrafted=1&draft_class=2023&draft_round=1`; assert
    only undrafted players returned (not 2023 picks).

- Undrafted checkbox appears in form (players subject only) and round-trips via URL.
  - Verify: browser — check the undrafted box, submit, confirm checkbox is still checked
    after the page updates.

---

## Phase 2 — Result grain selector

### 2a–2d. Grain parameter

- Default grain (`career`) produces identical results to the current Explorer.
  - Verify: integration test — query without `grain` param and with `grain=career`; assert
    identical rows and counts.
  - Expected: no regression for users who never touch the grain selector.

- `grain=per_competition` returns one row per player × summer league event.
  - Verify: integration test — seed one player with logs across two competitions; query
    with `grain=per_competition`; assert two rows, one per competition.
  - Expected: label reads `"PlayerName · VenueLabel Year"` for each row.

- `per_competition` grain reads from `summer_league_player_seasons`, not from raw logs.
  - Verify: unit test mocking the DB call — confirm `summer_league_player_seasons` is
    queried, not `summer_league_player_game_logs`, when grain is `per_competition`.

- `grain=per_game` returns one row per game log entry.
  - Verify: integration test — seed one player with 3 game logs; query `grain=per_game`;
    assert 3 rows, each with date + opponent in the label.
  - Expected: row links to the box-score page for that game.

- Mode selector (`per_game` / `per_36` / `per_100` / `totals`) is hidden in the UI for
  `per_game` grain.
  - Verify: browser — select Per game grain, confirm mode select is absent from the form.

- Min GP and Min MIN inputs are hidden for `per_game` grain.
  - Verify: browser — select Per game grain, confirm min-GP and min-MIN inputs are absent.

- Grain param persists through sort and page navigation.
  - Verify: browser — set grain to Per competition, sort by AST, go to page 2; confirm
    `grain=per_competition` is in the URL at each step.

- `per_competition` query uses indexes; no Seq Scan on large tables.
  - Verify: `make explain ROUTE=/stats/summer-league/explorer?grain=per_competition`
  - Expected: `ix_summer_league_player_seasons_year_venue` or `player_id` index used.

### 2e. Draft pick range filter

- `draft_pick_min` and `draft_pick_max` filter correctly.
  - Verify: integration test — seed players with picks 1, 5, 15, 30; query
    `?draft_pick_min=1&draft_pick_max=10`; assert only picks 1 and 5 returned.

- New `PlayerMaster.draft_pick` index confirmed via `make explain`.

### 2f. Country filter

- Country dropdown populates distinct non-null values from `PlayerMaster.birth_country`.
  - Verify: integration test — seed players with `birth_country = "US"` and `"FR"`;
    assert both appear as options in `ExplorerFacets.countries`.

- Filtering by country returns only players from that country.
  - Verify: integration test — `?country=US` returns only US-born players.

### 2g. Round type filter

- Round type dropdown populates from distinct `game.round_label` values.
  - Verify: integration test — seed games with distinct `round_label` values; assert facet
    lists them.

- Filtering by round type returns only players (or teams / games) from games with that
  round label.
  - Verify: integration test for each subject (players, teams, games) — seed qualifying
    and championship games; filter `?round_type=Qualifying`; assert only qualifying-game
    rows returned.

- New `SummerLeagueGame.round_label` index confirmed via `make explain` after adding the
  join to `_query_players`.
  - Expected: Index Scan on `summer_league_games.round_label`.

### 2h. Team filter for players

- `?team_slug=lakers` returns only players who appeared for that team.
  - Verify: integration test — seed two players on different team entries in the same
    competition; filter by one team; assert only that team's players returned.

- Team filter works in combination with year and venue filters.
  - Verify: integration test — two competitions, same team slug but different years; filter
    by team + year; assert correct competition's players only.

---

## Phase 3 — SQL-side sort and paginate

- Results for `career` grain are sorted in the database, not in Python.
  - Verify: unit test — mock the DB session and assert the SQL statement contains
    `ORDER BY` and `LIMIT` clauses.
  - Expected: `_paginate()` Python helper is no longer called for the players subject.

- Pagination counts and page boundaries are correct after the migration.
  - Verify: integration test — seed 55 players (page size 50); query page 1 → 50 rows;
    query page 2 → 5 rows; total = 55; `has_next` correct on each page.

- `per_game` grain paginates correctly in SQL (unbounded row count scenario).
  - Verify: integration test — seed a player with 60 game logs; page 1 returns 50 rows;
    page 2 returns 10.

- No regression in sort order: descending by default, ascending on toggle.
  - Verify: integration test comparing row order before and after Phase 3 migration for
    the same query parameters.

---

## Phase 4 — Advanced features

### 4a. Advanced metrics in Explorer

- For `grain=per_competition` with a single year + venue, PER / BPM / WS / VORP columns
  appear when `adv_eligible = True` for that competition.
  - Verify: integration test — seed `summer_league_player_seasons` rows with
    `adv_eligible=True` and composite values; query single year + venue + per_competition
    grain; assert composite columns in response.

- When `adv_eligible = False` for the selected pool, composite columns are absent or shown
  as `—`; a warning banner is visible.
  - Verify: browser — select a competition where `adv_eligible=False`; confirm warning
    message and no composite column values.

- Composite columns do not appear for multi-year or career grain queries.
  - Verify: integration test — query with year range + `grain=per_competition`; assert PER
    is absent from column catalog.

### 4b. CSV export

- `?format=csv` returns a `text/csv` response with `Content-Disposition: attachment`.
  - Verify: integration test — hit Explorer with `format=csv`; assert response status 200,
    content-type `text/csv`, and correct header row + data rows.

- CSV output contains the same rows as the HTML table for the same query.
  - Verify: integration test — compare JSON response (via a helper) to CSV parsed output;
    assert row count and values match.

- "Download CSV" link is visible below the results count in the Explorer UI.
  - Verify: browser — load results, confirm link present and triggers download.

### 4c. Age filter

- `age_min` and `age_max` filter players by age at time of summer league.
  - Verify: integration test — seed players with known birth dates and competition years;
    query `?age_min=19&age_max=21`; assert only players aged 19–21 at the relevant
    competition year are returned.

- Age filter works alongside other filters (year range, venue, draft class).
  - Verify: integration test with combined filters.

---

## Cross-cutting checks

- **URL shareability:** every filter state round-trips through the URL. Load a URL with
  all params set; assert form controls match URL params.
  - Verify: browser — construct a URL with all active filters + grain + sort + page;
    reload; confirm all form controls are pre-filled correctly.

- **JS-off fallback:** the form submits normally (full page load) when JS is disabled.
  - Verify: browser with JS disabled — submit form; confirm page reloads with results.

- **Subject toggle:** switching between Players / Teams / Games resets grain to `career`
  (or hides grain entirely for Teams/Games) and does not carry invalid sort keys.
  - Verify: browser — set grain to per_competition, switch to Teams subject, confirm no
    JS error and valid results.

- **Empty state:** filters that match nothing show a "No results" message, not an error.
  - Verify: integration test — query with filters that match zero rows; assert 200 status
    and empty message in response.

- **Performance budget:** the Explorer route stays within its query-count budget after all
  Phase 1–3 changes.
  - Verify: `make perf` for `/stats/summer-league/explorer` and
    `/stats/summer-league/explorer?partial=1`.
  - Expected: no new queries added vs. baseline; counts within budget.
