# Summer League — Player Page Section Spec

## Purpose

Surface each draft prospect's NBA Summer League box-score production directly on the
existing public player detail page (`/players/{slug}`), as a new "Summer League"
section beneath the College Production scoreboard.

This is the **first user-facing surface** of the Summer League data backbone
(`docs/plans/summer-league-data-backbone.md`). It deliberately ships **raw box-score
data plus lightweight rate transforms (per-36 and per-100)** — no composite/derived
metrics, no new ingestion, no standalone Summer League pages. It turns the populated data model
into product value at the single highest-traffic, lowest-risk touchpoint.

## Product Context

- The player page is DraftGuru's most-visited surface; the SL plans
  (`docs/plans/summer-league-stats-pages.md`) name `/players/{slug}#summer-league`
  as the **highest-traffic SL touchpoint**.
- It is the purest expression of the SL "moat": Summer League production stitched to a
  known draft prospect's profile (consensus rank, bio, college stats already on the
  page). No competitor co-locates these.
- It validates the data backbone end-to-end (resolution → read → display) with a small,
  reversible UI change before larger SL surfaces (hub, leaders, game box, draft-class)
  are built.
- It fits the draft-calendar pipeline strategy: a year-round reason to visit prospect
  pages between drafts.

See also: `docs/summer_league_stats_plan.md` (feature spec), `docs/summer_league_stat_inventory.md`
(stat tiers), `docs/summer_league_advanced_metrics_methodology.md` (why composites are deferred).

## Goals

1. For any player resolved to Summer League game logs, render a **Summer League
   scoreboard section** that mirrors the existing College Production component:
   - Per-season (per-year) headline averages: **PPG / RPG / APG**.
   - Detail grid: SPG, BPG, TOPG, MPG, GP, GS.
   - Shooting splits with bars: FG%, 3P%, FT%.
2. A **stat-mode toggle**: **Per Game** (default), **Per 36**, and **Per 100**.
   - Per-36 is a pure box-score rate transform (`stat * 36 / total_minutes`), available
     across the entire history (needs only minutes).
   - Per-100 uses NBA's supplied on-court `pace` (possessions/48), so it needs **no**
     possession model: `possessions = pace * minutes / 48`, then `stat * 100 / possessions`.
     `pace` is populated for **100% of played player logs in 2017–2025**; it is absent for
     2007/2010 and the sparse 2012–2016 years, where per-100 cells render `—`.
3. A **multi-year selector** (reuse the College scoreboard's season-toggle pattern) when
   a player has more than one SL year, plus a **Career** aggregate row across all SL years.
4. A compact **recent games** list (last N=5 resolved game lines: date, opponent, MIN,
   PTS, REB, AST, FG/3P).
5. **Graceful absence**: players with no resolved SL logs render exactly as today — the
   section is omitted entirely (same pattern as the consensus/combine sections).

## Non-Goals

- **No composite or derived "SL score" metrics** (the deferred M1/M2/M3 formulas).
- **No draft-class delta-vs-consensus, cohort percentile, model validation, or All-SL.**
- **No standalone SL pages** (landing, season hub, leaders, game box, explorer) — those
  are separate product slices.
- **No new ingestion or normalization.** Read-only against existing product tables.
- **No backfill of unresolved players.** Players not yet linked to a canonical record
  simply do not show the section.

## Source Data

Read-only against the normalized product tables (`app/schemas/summer_league.py`):

- `summer_league_player_game_logs` — one row per player per game. **Primary source.**
- `summer_league_games` — date, opponent, scores (for the recent-games list).
- `summer_league_team_entries` — opponent/team labels.
- `summer_league_competitions` — `year`, `venue_slug`, `display_name` (season grouping).

Linkage: `summer_league_player_game_logs.player_id → players_master.id`.

### Data Quality Reality (dev DB, observed 2026-06-13)

This scopes what the section can honestly show. Ground truth, not assumptions:

| Signal | Reality | Product implication |
|---|---|---|
| Reliable years | 2017, 2018, 2019, 2021–2025 (~2,000–2,800 logs each, all venues) + 2007, 2010 | Section is rich for recent prospects |
| Empty / sparse years | 2011 & 2020 empty; **2012–2016 sparse** (games exist, few player logs normalized) | A prospect whose only SL year is 2012–2016 may show little/nothing — acceptable; section is per-player |
| Box score | `pts/reb/ast/fg/3p/ft/stl/blk/tov/+-/minutes_seconds` populated | Core display is solid |
| DNP rows | ~30% of logs have null `pts` (Did Not Play) | **Exclude from averages and GP**; GP counts games with `minutes_seconds > 0` |
| Advanced metrics | `TS%, USG%, OffRtg, PIE` present for the ~16k rows with minutes (NBA-provided, not computed) | Optional; see Open Decisions. eFG%/TS% are "raw" (NBA-supplied) so usable without our own formula |
| `pace` (possessions/48) | Populated for **100% of played player logs 2017–2025**; absent 2007/2010 & sparse 2012–2016 | Enables Per-100 without a possession model; per-100 renders `—` where `pace` is absent |
| Player resolution | ~60% of logs (14,021/22,179) linked to a canonical player | **Section coverage is partial by design**; ~40% of logs not yet attributable. Improving resolution is a separate backbone effort |
| Minutes unit | `minutes_seconds` stored in **seconds** | Convert to minutes for MPG and per-36 denominator |

## Read Model

New stateless read service: `app/services/summer_league_stats_service.py`.

```python
async def get_summer_league_profile_by_player_id(
    db: AsyncSession, player_id: int
) -> PlayerSummerLeagueProfile | None
```

Returns `None` (or an empty profile) when the player has no resolved SL game logs, so
the route can omit the section.

### DTOs (dataclasses, `app/models/` or co-located)

- `PlayerSummerLeagueProfile`
  - `seasons: list[PlayerSummerLeagueSeason]` (descending year)
  - `career: PlayerSummerLeagueSeason` (aggregate across all years; `season_label="Career"`)
  - `recent_games: list[PlayerSummerLeagueGame]` (last 5 resolved games with minutes > 0)
- `PlayerSummerLeagueSeason`
  - `year: int`, `season_label: str`, `venues: list[str]` (e.g. `["Las Vegas", "California Classic"]`)
  - `gp: int`, `gs: int`, `total_minutes: float`, `total_possessions: float | None`
  - Per-game averages: `ppg, rpg, apg, spg, bpg, topg, mpg`
  - Shooting: `fg_pct, fg3_pct, ft_pct` (computed from summed makes/attempts, not averaged percentages)
  - Per-36 counterparts: `pts_per36, reb_per36, ast_per36, ...`
  - Per-100 counterparts: `pts_per100, reb_per100, ...` — `None` when `total_possessions` is
    unavailable (no `pace` for that season's games)
- `PlayerSummerLeagueGame`
  - `game_date, opponent_label, minutes, pts, reb, ast, fgm, fga, fg3m, fg3a, venue`

### Aggregation rules

- **Group by `competition` (year + venue/league)**, NOT by year alone. California Classic
  and Salt Lake City are distinct warm-up leagues that run before Las Vegas, with different
  rosters; a player who appears in two venues the same summer gets **one row per
  competition**. Tabs disambiguate with a venue tag only when a year has more than one stint
  (e.g. `2024 LV` + `2024 SLC`; a single-stint year stays `2024`). Within a year, marquee
  Las Vegas sorts first. The **Career** row still combines all competitions.
- **GP** = count of that year's logs with `minutes_seconds > 0`. **GS** = logs with a
  non-null `starter_position`.
- **Per-game averages** = `sum(stat) / GP`, over non-DNP logs only.
- **Shooting %** = `sum(makes) / sum(attempts)` (weighted), with a `—` em-dash when
  attempts are 0. Never average per-game percentages.
- **Per-36** = `sum(stat) * 36 / total_minutes` where `total_minutes = sum(minutes_seconds)/60`;
  guard against `total_minutes == 0`.
- **Per-100** = `sum(stat) * 100 / total_possessions` where
  `total_possessions = Σ over games (pace_g * minutes_g / 48)`, summed only over games with
  non-null/non-zero `pace`. When no game in the span has `pace`, `total_possessions` is `None`
  and all per-100 fields are `None` (rendered `—`).
- **Career row** aggregates across all years with the same rules.

### Performance / indexing

The route queries `summer_league_player_game_logs WHERE player_id = ?`. The existing
index is composite `(competition_id, player_id)` — its leading column is `competition_id`,
so a `player_id`-only filter cannot use it efficiently. **Add a single-column index on
`player_id`** (SQLModel + Alembic migration in the same change) and verify via
`make explain ROUTE=player-detail` on a prod-like branch (Index Scan, not Seq Scan).
Re-check the `/players/{slug}` query budget with `make perf`; bump the budget consciously
in `tests/integration/perf/budgets.py` for the one added query if needed.

## UI / Section Design

- **Placement:** Directly below the College Production scoreboard, above the Consensus
  Rank section. Use the same retro "stats-scoreboard" visual treatment (scanlines, pixel
  corners) per the style guide.
- **Component reuse:** Mirror the College Production scoreboard markup
  (`stats-headline-row` PPG/RPG/APG + `stats-detail-grid` + `shooting-splits`) so it reads
  as a sibling, not a new pattern.
- **Year selector:** Reuse the `season-btn` / `.season-data[data-season-index]` toggle
  and the `CollegeStatsModule` JS pattern; add a `SummerLeagueStatsModule` cloned from it.
  Add a "Career" pseudo-season as the last toggle.
- **Stat-mode toggle:** A small Per Game / Per 36 / Per 100 segmented control scoped to the
  section; flips which set of cells is shown (server-renders all three, JS toggles visibility
  — no fetch). Per-100 cells show `—` for seasons without `pace`.
- **Venue + season context bar:** "2024 · Las Vegas + California Classic · 7 GP".
- **Recent games:** A compact 5-row mini-table under the averages.
- **Empty state:** Section omitted via `{% if summer_league %}` — no placeholder, matching
  the consensus/combine convention.
- **Data injection:** Pass via template context and `window.SUMMER_LEAGUE_DATA = {{ ... | tojson | safe }}`
  consistent with existing `window.PLAYER_DATA` injection.

## Edge Cases

- Player with SL logs but all DNP in a year → that year shows GP=0 and is suppressed.
- Player resolved but only sparse-year data (2012–2016) → show whatever exists.
- Multi-venue single year → one merged row, venue list in context bar.
- `total_minutes == 0` → per-36 cells render `—`, no division.
- Unresolved player (`player_id` null on all their logs) → section absent (expected).
- Multi-year player → multiple year toggles + Career row.

## Required Tests

- **Integration** (`tests/integration/`):
  - `/players/{slug}` renders the SL section for a player seeded with resolved SL logs;
    headline PPG/RPG/APG match expected aggregates.
  - Section is **absent** for a player with no SL logs (page still 200s, renders normally).
  - Multi-venue same-year logs aggregate into one season row with both venues listed.
  - DNP logs excluded from GP and averages.
  - Per-36 values correct for a known minutes/stat fixture.
  - Per-100 values correct for a known pace/minutes/stat fixture; per-100 is `—`/absent for a
    pre-2017 season lacking `pace`.
- **Unit** (`tests/unit/`):
  - Aggregation + per-36 + per-100 (pace-based possessions) + weighted shooting-% pure
    functions, including zero-minute, zero-possession, and zero-attempt guards.
- **Visual** (`make visual`): screenshot of a player page with the SL section (per-game and
  per-36 modes); verify it matches the College scoreboard treatment.
- **Perf** (`make perf` + `make explain ROUTE=player-detail`): new query uses an Index Scan
  on `player_id`; `/players/{slug}` query budget updated.

## Ticket Breakdown

1. **Write Summer League Player-Page Section Spec** — this document. (Done in-session.)
2. **Add Summer League player-stats read service + index** — new
   `summer_league_stats_service.py`, DTOs, aggregation/per-36 logic, single-column
   `player_id` index + Alembic migration. Unit tests for aggregation. *(Depends: 1)*
3. **Wire SL profile into the player-detail route** — call the service in
   `app/routes/ui.py:player_detail`, add `summer_league` to template context + window data.
   Integration test: section present/absent. *(Depends: 2)*
4. **Build the Summer League scoreboard section (template + CSS + JS)** — new section in
   `player-detail.html`, styles in `player-detail.css`, `SummerLeagueStatsModule` in
   `player-detail.js` (year + per-mode toggles, recent games). *(Depends: 3)*
5. **Tests + verification pass** — integration coverage for aggregation/edge cases, visual
   screenshot, perf/explain. *(Depends: 4)*

### Dependency Graph

```
1 → 2 → 3 → 4 → 5
```

(2's read service and 4's UI could partly parallelize once DTO shapes are fixed in 2.)

## Open Decisions

1. **Per-100 mode** — **Include in v1.** Feasible and lightweight via NBA's supplied `pace`
   (100% of played logs 2017–2025); no possession model needed. Renders `—` for pre-2017
   seasons that lack `pace`. _(Resolved 2026-06-13.)_
2. **Advanced stats display (TS%/eFG%/USG%)** — These are NBA-provided (raw, not computed
   by us) and present for ~16k logs. Recommend surfacing **TS%** only, as a single context
   stat with a one-line "Summer League context" caveat, OR omit entirely for v1 to keep the
   section strictly box-score. Lean: include TS% (cheap, high signal); confirm at build.
3. **Section placement** — Recommend below College Production / above Consensus. Confirm
   during visual QA.

## Completion Bar

The slice is product-complete when:
1. A resolved multi-year SL player (e.g. a 2023–2025 prospect) shows a Summer League
   section with correct per-year and Career averages, working year + Per-Game/Per-36
   toggles, and a recent-games list.
2. A player with no SL logs renders identically to today (section absent, page 200s).
3. The new `player_id` query is index-backed and within the `/players/{slug}` perf budget.
4. Integration, unit, and visual tests pass; `make precommit` and `mypy app` are clean.
