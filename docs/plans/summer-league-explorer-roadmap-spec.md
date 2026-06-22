# Summer League Explorer — Feature Parity Spec

**Status:** Draft (planning)
**Branch:** `claude/summer-league-explore-roadmap-1ci6mm`
**Author origin:** Roadmap session 2026-06-21
**Companion docs:**
- `docs/plans/summer-league-games-index-spec.md` (shipped games index)
- `docs/plans/summer-league-stats-pages.md` (original stats plan)
- `docs/plans/ai-orchestrator-ticket-spec.md` (orchestrator defaults)

---

## 1. What this is (and is not)

The **Summer League Explorer** (`/stats/summer-league/explorer`) is a Stathead-style
faceted query builder that turns URL-encoded filters into a sortable, paginated
table of player, team, or game results across every Summer League season.

This spec defines what it takes to bring the Explorer to **functional parity with
Basketball Reference / Stathead's player-finder** — richer filter dimensions, multiple
result grains, a more complete stat column set, and correct DB indexing throughout.

- **Is:** an incremental improvement to an existing, working page — no new tables,
  no new routes, no new templates (beyond template edits).
- **Is not:** a rebuild. The current architecture (URL-only state, JS-enhanced partial
  refresh, three subjects) is sound and is kept as-is.
- **Is not:** a leaders page or advanced-metrics-first surface. The leaders page
  (`/stats/summer-league/leaders`) already handles PER/BPM/WS; the Explorer stays
  additive box stats and ratios *except* where the `per_competition` grain can cheaply
  pull from `summer_league_player_seasons`.

---

## 2. Current state (baseline)

### What works today

**Players subject:**
- Filters: year range (`year_min`/`year_max`), venue, draft class, draft round, min GP,
  min minutes
- Display modes: per game, per 36, per 100, totals
- Columns: GP, MIN, PTS, REB, AST, STL, BLK, TOV, FGM, FGA, 3PM, 3PA, FTM, FTA,
  FG%, 3P%, FT%, TS%
- Sort by any column asc/desc; 50 rows/page; shareable URL; JS partial refresh

**Teams and Games subjects:** service-implemented and functional (W/L/PPG/DIFF/PACE/ORtg/DRtg
for teams; matchup + Total/Margin for games).

### Active bug

`PlayerMaster.draft_round` is an active WHERE-clause filter with no database index.
This ships as the very first change.

---

## 3. Phases

### Phase 1 — Quick wins / bug fixes

No new tables. No new migrations beyond the index additions.

#### 1a. Fix `draft_round` index (active bug)

Add `index=True` to `PlayerMaster.draft_round` in `app/schemas/players_master.py`
and generate an Alembic migration.

#### 1b. Add missing stat columns

Extend `_PLAYER_STAT_COLUMNS` and `_compute_player_values` in
`app/services/summer_league_explorer_service.py`:

| New column | Key | Source field | Notes |
|-----------|-----|-------------|-------|
| Off Reb | `oreb` | `player_game_log.oreb` | Scales like other counting stats |
| Def Reb | `dreb` | `player_game_log.dreb` | Scales like other counting stats |
| Personal fouls | `pf` | `player_game_log.pf` | Scales like other counting stats |
| Plus/minus | `plus_minus` | `player_game_log.plus_minus` | Totals only for `per_game`/totals; skip for per_36/per_100 |
| eFG% | `efg_pct` | calculated | `(FGM + 0.5 × 3PM) / FGA × 100` |

The query in `_query_players` must `SUM` the new columns alongside existing ones.
The `_COUNTING` tuple must include `oreb`, `dreb`, `pf`. `plus_minus` is a signed
counting stat — sum it and display per-game only; suppress in per_36/per_100 modes
(it does not pace-normalize meaningfully). `efg_pct` is computed from summed makes/attempts.

#### 1c. Position filter

1. Add `index=True` to `PlayerMaster.position` in `app/schemas/players_master.py`.
2. Add `position: Optional[str]` to `ExplorerQuery`.
3. Add `position` to `parse_query()`.
4. Add the WHERE clause in `_query_players`: `pm.position == q.position` when set.
5. Add `positions` list to `ExplorerFacets` (populated from distinct `PlayerMaster.position`
   values where not null, sorted).
6. Add `get_facets()` query for distinct positions.
7. Render a `<select>` for position in `explorer.html` (players subject only).
8. Carry `position` through `explorer_qs()` macro in `_explorer_results.html`.
9. Alembic migration for the new index.

Standard position values expected: `G`, `F`, `C`, `G-F`, `F-C` etc. — no normalisation
needed, just pass-through filter.

#### 1d. Undrafted toggle

Add an `undrafted` boolean param (`?undrafted=1`). When set, filter `pm.draft_year IS NULL`.
Mutually exclusive with `draft_class` and `draft_round` (if `undrafted=1`, ignore those).
Expose as a checkbox in the form below the draft-round select.

---

### Phase 2 — Result grain selector

This is the highest-value missing feature: the ability to see **one row per player per
summer league event** (or per game) rather than a single career aggregate.

#### 2a. Add `grain` parameter

Add `grain: str` (default `"career"`) to `ExplorerQuery` with three values:

| Value | UI label | Row count | Data source |
|-------|----------|-----------|-------------|
| `career` | Career | 1 per player | Aggregated from `summer_league_player_game_logs` (current) |
| `per_competition` | Per competition | 1 per player × SL event | `summer_league_player_seasons` (materialized) |
| `per_game` | Per game | 1 per game log | `summer_league_player_game_logs` (no aggregation) |

#### 2b. `per_competition` grain (priority)

- Read from `summer_league_player_seasons` joined to `summer_league_competitions` and
  `players_master` for name/slug.
- All existing filters (year range, venue, draft class/round, position) apply via the
  `year`/`venue_slug` columns already on `summer_league_player_seasons`.
- Min GP and min minutes thresholds apply as before (`gp >= min_games`,
  `minutes >= min_minutes`). Note: `minutes` on `summer_league_player_seasons` is in
  minutes (not seconds like the game-log `minutes_seconds`).
- The label column reads `"Player · Venue Year"` (e.g. `"Chet Holmgren · Vegas 2022"`).
- Available columns: all Phase 1 columns plus the materialized rates already on
  `summer_league_player_seasons` (ts_pct, efg_pct, usg_pct, ast_pct, etc.) where
  `adv_eligible` is not required (these are derived from box totals, not recalibrated
  composites).
- Index coverage: `ix_summer_league_player_seasons_player_id` and
  `ix_summer_league_player_seasons_year_venue` already exist — no new migrations needed.

#### 2c. `per_game` grain

- Read directly from `summer_league_player_game_logs` (no aggregation).
- Filters: year/venue via `competition_id → competition`, draft class/round/position via
  `player_id → players_master`.
- `min_gp` / `min_min` thresholds are hidden in the UI and ignored for this grain.
- Label reads `"Player · Date · Opp"`. Opponent resolved by joining to
  `summer_league_team_entries` via `team_entry_id` (the other team on the same game).
- Link goes to the box-score page (`/stats/summer-league/{year}/games/{game_id}`).
- Index coverage: `ix_summer_league_player_game_logs_competition_player` covers
  `(competition_id, player_id)` — already exists ✓.

#### 2d. UI wiring

- Add a `grain` `<select>` (Career / Per competition / Per game) to the form, players
  subject only, placed before the Mode selector.
- Mode selector (`per_game` / `per_36` / `per_100` / `totals`) is hidden for `per_game`
  grain (raw single-game stats are already per-game).
- Min GP / Min MIN inputs are hidden for `per_game` grain.
- `explorer_qs()` macro carries `grain` through sort/page links.
- `ExplorerQuery.sort` default resets to `pts` for all grains; valid sort keys are the
  same column catalog for `career` and `per_competition`; for `per_game` same keys minus
  `gp` (not meaningful per row).

---

### Phase 2 (continued) — Additional filter dimensions

#### 2e. Draft pick range filter

Add `draft_pick_min: Optional[int]` and `draft_pick_max: Optional[int]` to `ExplorerQuery`.
Add `index=True` to `PlayerMaster.draft_pick` + Alembic migration.
Render as two number inputs in the form (players subject only), placed alongside the
existing draft class/round filters.

#### 2f. Country / birth country filter

`PlayerMaster.birth_country` already has `index=True`. Add `country: Optional[str]` to
`ExplorerQuery`. Populate `ExplorerFacets.countries` from distinct non-null
`birth_country` values (sorted). Render as a `<select>` (players subject only).

#### 2g. Round type filter

Filter games by tournament round (`game.round_label`). Applies to all three subjects
(players, teams, games) by filtering the underlying game rows.

1. Add `round_type: Optional[str]` to `ExplorerQuery`.
2. In `_query_players`, add a join to `summer_league_games` via
   `player_game_log.game_id` and filter `game.round_label == q.round_type` when set.
3. In `_query_teams` and `_query_games`, add the same filter.
4. Add `index=True` to `SummerLeagueGame.round_label` in `app/schemas/summer_league.py`
   + Alembic migration.
5. Populate `ExplorerFacets.round_types` from distinct non-null `round_label` values.
6. Render as a `<select>` in the form (all subjects).

**Note on `_query_players` performance:** adding the game join for round filtering means
the critical aggregation query now joins four tables (`player_game_log → competition →
players_master → game`). Verify the query plan after adding this join; the existing
`ix_summer_league_player_game_logs_competition_player(competition_id, player_id)` should
still be the driving index.

#### 2h. Team filter for players

Show only players who appeared for a given NBA franchise in summer league.

Join `summer_league_team_entries` via `player_game_log.team_entry_id` and filter by
`team_entry.team_slug`. Add `team_slug: Optional[str]` to `ExplorerQuery`. Populate
`ExplorerFacets.teams` from distinct slugs (sorted). No new index needed —
`ix_summer_league_team_entries_competition_team_slug(competition_id, team_slug)` exists.

---

### Phase 3 — SQL-side sort and paginate

**Problem:** `_paginate()` fetches all matching rows into Python, sorts in Python, then
slices to the page. For the players aggregation query this is currently fine (hundreds
of rows); it will degrade as data grows or as per_game grain produces thousands of rows.

**Fix:** Replace Python sort + slice with SQL `ORDER BY ... LIMIT ... OFFSET ...` for
the players subject.

- The aggregation query already runs in the database. Add `.order_by(...)` and
  `.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)` to the SQLAlchemy statement.
- A separate `COUNT(*)` subquery or `SELECT count(*) OVER ()` window gives the total
  for pagination display.
- `per_game` grain is the most important case to fix (unbounded rows per filter).
- Teams and games subjects are lower priority (smaller datasets) but should follow the
  same pattern for consistency.
- Remove `_paginate()` helper once all subjects are migrated; it becomes dead code.

---

### Phase 4 — Advanced features

#### 4a. Advanced metrics mode in Explorer

For `per_competition` grain with a single venue and single year selected, expose
materialized composites from `summer_league_player_seasons`: PER, ORtg, DRtg, BPM,
WS, VORP. Gate behind `adv_eligible = True`. Show a warning banner when composites are
unavailable for the selected pool.

This should be a separate mode column-set rather than mixing composites into the existing
column catalog — follow the leaders page pattern.

#### 4b. CSV export

Add `?format=csv` to the Explorer route. When set, stream a `text/csv` response using
the same query pipeline — no new DB reads. Add a "Download CSV" link below the results
count in `_explorer_results.html`.

#### 4c. Age filter

Compute player age at time of summer league from `PlayerMaster.birth_date` and the
competition `year`. Add `age_min: Optional[int]` / `age_max: Optional[int]` to
`ExplorerQuery`. For the `career` grain, use the earliest or average competition year
to compute age — document the choice. No index needed (age is a computed value, not a
stored column).

---

## 4. Implementation map

### Files that change across all phases

| File | Change |
|------|--------|
| `app/schemas/players_master.py` | Add `index=True` to `draft_round`, `position`, `draft_pick` |
| `app/schemas/summer_league.py` | Add `index=True` to `SummerLeagueGame.round_label` |
| `alembic/versions/` | One migration per index addition (or batch into two: players_master indexes, SL game index) |
| `app/services/summer_league_explorer_service.py` | All query, facet, and DTO changes |
| `app/templates/stats/summer-league/explorer.html` | New form controls |
| `app/templates/stats/summer-league/_explorer_results.html` | `explorer_qs()` macro updates |

### New integration tests

Mirror `tests/integration/test_summer_league_explorer.py`. Add cases per Phase.

---

## 5. Out of scope

- The **leaders page** (`/stats/summer-league/leaders`) — no changes; advanced metrics
  stay there for career/single-competition views.
- Multi-column sort (clicking a secondary sort key).
- Saved searches / bookmarked queries.
- Share-card PNG export for Explorer results.
- Non-SL leagues (G-League, college).
- Admin-facing controls.

---

## 6. Open questions

1. **`plus_minus` scaling:** should +/- be shown only in totals mode, or also per-game?
   (Lean: per-game only — total +/- is meaningful but per-36/per-100 +/- is not standard.)
2. **`per_competition` column set:** should it expose all materialized rate columns from
   `summer_league_player_seasons` (usg_pct, ast_pct, orb_pct, etc.) even without the
   advanced composites? (Lean: yes — they're box-derived, not pool-calibrated.)
3. **Round type facet values:** `round_label` values in the DB (`"Qualifying"`,
   `"Semifinal"`, `"Championship"` etc.) should be confirmed from actual data before
   rendering the dropdown — don't hard-code labels.
4. **SQL paginate migration (Phase 3):** should `per_competition` skip `_paginate()` from
   the start (since it reads from `summer_league_player_seasons` and can trivially add
   `ORDER BY + LIMIT`)? (Lean: yes — implement SQL-side pagination for `per_competition`
   in Phase 2 and retrofit `career` in Phase 3.)
5. **Grain selector placement:** should `grain` replace the subject toggle (players /
   teams / games) or live inside the filter form? (Lean: inside the filter form, players
   subject only — subject toggle stays as-is.)

---

## 7. Definition-of-Done notes

- `make precommit` passes (ruff + mypy) for every PR.
- `mypy app --ignore-missing-imports` exits clean.
- `pytest tests/unit -q` passes; `pytest tests/integration -q` passes.
- `make coverage.diff` ≥ 80% patch coverage on changed `app/` lines.
- Each new or changed index is verified with `make explain ROUTE=/stats/summer-league/explorer`
  against a prod-like DB before the PR merges — Index Scan confirmed, no Seq Scan on large tables.
- For `per_game` grain and the round-type join, re-run `make explain` after each addition
  since the query shape changes materially.
- No visual changes in Phase 1–3 beyond form controls; `make visual` for any Phase 4 UI.

---

## 8. Decision log

- **2026-06-21** — Roadmap scoped in conversation. Confirmed: Explorer is read-only
  incremental; no new tables. `draft_round` missing index identified as active bug.
- **2026-06-21** — `grain` selector (per_competition / per_game pivots) added as Phase 2
  priority after noting the Explorer is hardwired to career-aggregate grain — a meaningful
  gap vs. Stathead. `per_competition` prioritized because `summer_league_player_seasons`
  is already materialized.
- **2026-06-21** — Advanced composites (PER/BPM/WS/VORP) deferred to Phase 4 in Explorer;
  leaders page already serves them. Only box-derived rates in scope for Phases 1–3.
