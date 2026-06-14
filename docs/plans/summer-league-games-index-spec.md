# Summer League Games Index — Tech Spec

**Status:** Draft (planning)
**Branch:** `feature/summer-league-game-blog`
**Author origin:** Brainstorm 2026-06-13
**Companion docs:** `docs/plans/summer-league-stats-pages.md` (page #9 box score; this spec adds the missing global games index), `docs/summer_league_stats_plan.md`, `docs/plans/summer-league-player-page-section-spec.md` (shipped player-page SL section).

---

## 1. What this is (and is not)

A **centralized, searchable box-score store** for Summer League: one place to browse and filter
**every SL game across all years and venues**, with each game opening to its full box score, plus a
**per-player game-log drill-down** (a "Game Log" button on the player page that opens a player's complete
SL game-by-game logs). Games are cross-linked between all three surfaces.

- **Is:** a stats-aggregator surface — a Basketball-Reference-style "box score index" for SL.
- **Is not:** an editorial/blog surface. No recaps, no narrative, no AI-written content. (The early
  "game blog" framing was dropped — see decision log.) This keeps us inside DraftGuru's
  "analytical aggregator, not a recap site" positioning with zero tension.

**Scope guardrails (decided 2026-06-13):**
- **Lightweight index**, not the heavy faceted Explorer (page #11). The Explorer remains a separate,
  later project. Where practical, URL/filter params should be chosen so the Explorer *could* subsume
  this later, but we do not build for it now.
- **Summer League venues only** — Vegas, Orlando, Salt Lake/Utah, California Classic (the LeagueIDs we
  already ingest). "All leagues" = all SL competitions/venues, not G-League/college.

---

## 2. Data readiness — no schema work

Everything required already exists in `app/schemas/summer_league.py`. This is a **read/query + templating**
feature with **no new tables and no migration**.

| Need | Source |
|------|--------|
| Game list (date, scores, status, quality) | `SummerLeagueGame` |
| Year + venue/league grouping | `SummerLeagueCompetition` (`year`, `venue_slug`, `league_id`) |
| Team names / abbreviations | `SummerLeagueTeamEntry` (+ optional `nba_teams` link) |
| Per-player box score | `SummerLeaguePlayerGameLog` (full traditional + advanced cols) |
| Per-team box score / totals | `SummerLeagueTeamGameLog` |
| Player → game linkage | `SummerLeaguePlayerGameLog.player_id` (resolved canonical) |

Relevant existing indexes: `ix_summer_league_games_competition_date`, `..._home_team_entry_id`,
`..._away_team_entry_id`; player logs unique `(game_id, person, team)` (covers `game_id` lookups) and
`ix_..._player_game_logs_player_id`. **Perf still to be verified** per repo rule (§7).

---

## 3. Surfaces

### 3.1 Games Index — `/stats/summer-league/games` (NEW; the "root document")

The centralized browser. Sortable, filterable table of games spanning all years/venues.

**Filters** (structured controls, not free text; state encoded in URL for shareable views):
- **Year** — scoreboard-strip year picker (shared component from the stats-pages plan); supports "All years".
- **Venue** — chips: Vegas · Salt Lake/Utah · California Classic · Orlando.
- **Team** — dropdown of team entries (scoped to selected year(s)/venue when set).
- **Player** — name typeahead; filters to games that player appeared in (join through player logs).
- **Date range** — optional.
- **Data quality** — optional chip filter (`full` / `partial` / `box_only` / `raw_only`).

**Table columns:** Date · Venue · Matchup (`Away @ Home`) · Final score · Status · Data-quality badge.
- Default sort: **most recent first**. Sortable by date and score.
- Each row links to the box-score page (§3.2).
- Pagination (server-side); min/sane page size.
- Data-quality dimming/badge per the stats-pages "Confidence dimming" convention.

**Empty/edge states:** no-results message when filters match nothing; clear-filters affordance.

### 3.2 Game Box Score — `/stats/summer-league/{year}/games/{game_id}` (plan page #9, box-only tier)

The document each index row opens into. Box-only version first (PBP/shot-chart enrichments deferred —
most SL games are `box_only`/`partial`).

- **Header:** matchup, final score, date, venue, data-quality badge.
- **Two team box scores** side by side from `SummerLeaguePlayerGameLog`, with a **team totals** row
  from `SummerLeagueTeamGameLog`.
- **Per-mode toggle:** Totals (default) · Per Game · Advanced — reuse stat-table chrome conventions.
- **Cross-links:** player name → `/players/{slug}`; team name → team-season page *(later; plain text until built)*;
  year → season hub *(later)*.
- Deferred (not in v1): line scores by quarter, lineup combos, on/off, shot chart, game flow (all PBP-gated).

### 3.3 Player-page hook (extend shipped SL section)

The player SL section already renders a `recent_games` list (`player-detail.html` +
`summer_league_stats_service.py`).
- Make each `recent_games` card link to its box-score page (§3.2).
- Add a **"Game Log"** button → the player game-log page (§3.4) — the per-player drill-down.
- Add a **"View all games"** CTA → `/stats/summer-league/games?player={slug|id}` (player-prefiltered index).

### 3.4 Player game logs — `/players/{slug}/summer-league` (plan page #17, generalized)

The "Game Log" button's destination: a player's **complete** SL game-by-game logs — every game across
every SL season, the box-score detail the player-page section only summarizes.

- **Header:** player chip + "Summer League · all seasons" + career totals strip.
- **Game-log table**, grouped by season (year · venue): one row per game — Date · Opp · Venue · MIN ·
  PTS/REB/AST · FG · 3P · FT · STL · BLK · TOV · +/− (advanced cols behind the per-mode toggle). Cross-link
  each opponent and date → box-score page (§3.2).
- **DNP handling** per the shipped section's rules: exclude `minutes_seconds = 0` / null-`pts` rows from GP
  and averages (`summer-league-player-page-section-spec.md`).
- The plan's #17 was per-year (`/players/{slug}/summer-league/{year}`); generalized here to **all seasons by
  default** to match "complete," with seasons as table groups. A `?year=` filter can scope to one season and
  preserves the plan's per-year URL intent (see open questions).
- Reuses the same box-score query layer as §3.2 (single source for per-game player logs).

---

## 4. Implementation sketch

- **Service:** `app/services/summer_league/games_index.py`
  - `search_games(session, *, years, venue_slugs, team_entry_id, player_id, date_from, date_to, quality, sort, page) -> paginated games`
  - `get_game_box_score(session, game_id) -> game + both teams' player logs + team totals`
  - `get_player_game_logs(session, player_id, *, year=None) -> per-game logs grouped by season` (§3.4)
  - Reuse/extend the existing `summer_league_stats_service` query helpers where they fit; §3.2 and §3.4
    share the per-game player-log query.
- **Routes:** add to `app/routes/ui.py` (or a new `app/routes/summer_league.py`) — three UI routes (games
  index, game box score, player game logs) rendering templates; pass `request` in context.
- **Templates:** `app/templates/stats/summer-league/games.html` (index), `.../game-detail.html` (box score),
  and `app/templates/players/summer-league-logs.html` (player game logs); reuse stat-table chrome and the
  year-picker partial.
- **Static:** page-scoped `summer-league-games.css` / `.js` for filter interactions (kebab-case, BEM, `DOMContentLoaded` init) — no build step.
- **Filter state:** encode in query params; JS updates URL so views are shareable/back-button friendly.

No new Pydantic persistence models; internal DTOs as dataclasses per service-layer conventions.

---

## 5. Out of scope (v1)

- Editorial/blog content of any kind.
- The faceted **Explorer** (page #11) and its stat min/max predicates, saved-query builder, CSV export.
- PBP-gated box-score enrichments (lineups, on/off, shot charts, game flow, quarter line scores).
- Non-SL leagues (G-League, college).
- Share-card PNG export for games (plan page #18) — can follow later.
- Team-season and season-hub pages (cross-links degrade to plain text until those ship).

---

## 6. Open questions

1. **Player filter input:** typeahead by name vs. arriving only via player-page CTA (id in URL)? (Lean: support both; CTA is the primary path.)
2. **Default year on first load:** most recent season vs. "All years"? (Lean: most recent.)
3. **Score sort meaning:** total points, margin, or omit score-sort in v1? (Lean: date sort only in v1; revisit.)
4. **Pagination vs. infinite scroll** for the index. (Lean: classic pagination — simpler, shareable URLs.)
5. **Player game-log URL:** `/players/{slug}/summer-league` (all seasons, default) with optional `?year=`,
   vs. the plan's per-year `/players/{slug}/summer-league/{year}`. (Lean: all-seasons default to match
   "complete"; keep a year filter. Confirm before building routes.)

---

## 7. Definition-of-Done notes specific to this feature

- **Perf rule applies (new plumbing):** the index and box-score routes issue new queries. Run `make perf`
  for the routes and `make explain ROUTE=<page>` against a prod-like DB for each new query; confirm Index
  Scans (esp. the player-filtered join and the per-game box-score fetch). Add indexes + migration in the
  same change if any are missing.
- Standard gates: `make precommit`, `mypy app --ignore-missing-imports`, unit + integration tests,
  `make coverage.diff` ≥80% patch, `make visual` for the two new pages.

---

## 8. Decision log

- **2026-06-13** — "Game blog" reframed to a **searchable box-score store** (no editorial content) after
  brainstorm. Confirmed: lightweight Games index (not the Explorer), Summer League venues only,
  authoring/editorial-voice questions moot (no content).
- **2026-06-13** — Added the **player game-log drill-down** (§3.4) to scope per user's notes: a "Game Log"
  button on the player page opening a player's complete SL game-by-game logs. This is plan page #17,
  generalized from per-year to all-seasons-by-default. Three surfaces total (index, box score, player logs).
