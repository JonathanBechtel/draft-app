# Summer League PBP & Shot Charts QA Checklist

**Sources:**
- Tech spec: `docs/plans/summer-league-pbp-shotchart-plan.md`

**Sibling artifact:** test plan at `summer-league-pbp-shotchart-test-plan.md`

This checklist defines product-level behaviors QA should verify before considering the Summer League play-by-play / shot-chart feature complete. Scope follows the spec: **Phase 1 (shot charts + shot profile)** and **Phase 2 (lean PBP: game flow, assisted-FG%)**. On/off and lineup stats (Phase 3) are out of scope and should NOT appear in the UI.

Conventions referenced: `docs/plans/ai-orchestrator-ticket-spec.md` (login recipe, dev server, screenshot paths), `docs/style_guide.md` (visual language).

---

## Core User Behaviors

### Shot charts (Phase 1)

- A user viewing a resolved player's SL section sees a shot chart for each SL competition with enough attempts.
  - Verify: navigate to `/players/{slug}` (a player with parsed SL shot data, e.g. a 2024 Vegas participant) and to `/players/{slug}/summer-league/{year}`.
  - Expected: a zone-heat half-court renders; each zone shows `FG% (FGA)`; zone fill color reflects FG% vs. the competition pool average (green above / red below).
  - Evidence: browser screenshot under `tests/visual/screenshots/`.

- A user can toggle the shot chart between zone-heat and raw shot dots.
  - Verify: click the chart's view toggle.
  - Expected: raw dots overlay the court (filled = make, hollow = miss) at their `loc_x/loc_y`; toggling back restores zone heat. Works with JS only; no full-page reload.
  - Evidence: before/after screenshots.

- A user viewing a game box score sees per-team and per-player shot charts where shot data exists.
  - Verify: navigate to `/stats/summer-league/games/{game_id}` for a game with parsed shot data; switch the team/player selector.
  - Expected: the chart re-scopes to the selected team or player using only that game's shots.
  - Evidence: screenshot.

- A user sees shot-diet columns (rim rate, mid rate, three rate, corner-3 rate) on the player SL surfaces.
  - Verify: inspect the player SL section / per-season table.
  - Expected: rates are percentages of FGA, sum coherently, and match the underlying shot rows for that player-competition.
  - Evidence: spot-check against `summer_league_shot_events` aggregation in the DB.

### Lean PBP (Phase 2)

- A user viewing a game box score (PBP-era game) sees a game-flow chart.
  - Verify: navigate to `/stats/summer-league/games/{game_id}` for a game at/after the confirmed PBP floor.
  - Expected: a score-margin-over-time line spanning all periods; endpoints match the final score; lead changes are visible.
  - Evidence: screenshot.

- A user viewing a resolved player's SL surface sees an assisted-FG% (self-creation) figure.
  - Verify: inspect the player SL section / per-season page for a PBP-era competition.
  - Expected: "% of made FGs assisted" displays; value equals `ast_fgm / (ast_fgm + unast_fgm)` derived from PBP events.
  - Evidence: DB spot-check against parsed PBP made-FG events with/without an assister.

### Sample-size & data-quality discipline (cross-cutting)

- Rate stats and shot charts carry the existing sample-size affordances.
  - Verify: view a player-competition below the attempts/minutes floor.
  - Expected: a `GP · MIN` (and FGA where relevant) badge is present; the zone chart is suppressed below the minimum-attempts floor (spec proposes ≥20 FGA), replaced by the table-only view with a "small sample" note. No chart implies false signal from 4 shots.
  - Evidence: screenshot of a low-sample player.

- Shot-chart zone coloring is calibrated to the competition pool, and that's communicated.
  - Verify: inspect the chart caveat chip.
  - Expected: a chip/label states the comparison baseline is the SL pool (not NBA), consistent with the bottom-up calibration philosophy.
  - Evidence: screenshot.

---

## Persistence And Data Integrity

- The shot-event parser persists one row per shot attempt, idempotently.
  - Verify: run the shot-event normalization stage twice for the same `(year, league_id)` slice.
  - Expected: `summer_league_shot_events` row count is identical after the second run; no duplicates. Idempotency keyed on `(nba_stats_game_id, nba_stats_game_event_id)`.
  - Evidence: DB count before/after; unique-constraint present.

- Shot events resolve to `PlayerMaster` where possible and store unresolved shots without dropping them.
  - Verify: inspect parsed rows for a slice with known unresolved source players.
  - Expected: resolved shots carry `player_id`; unresolved shots are stored with `player_id IS NULL` (and a `source_player_id` / `nba_stats_person_id`), not discarded.
  - Evidence: DB query counting null vs non-null `player_id`.

- Parsing updates the raw-file audit and competition availability flags from *parsed content*, not mere file presence.
  - Verify: parse a slice; inspect `summer_league_raw_files` and `summer_league_competitions`.
  - Expected: the `shotchartdetail` (and `playbyplayv2`) `parse_status` advances to a parsed state; `shotchart_available` / `pbp_available` reflect rows actually parsed (a present-but-empty file does not set the flag true).
  - Evidence: DB rows.

- The PBP-event parser persists events idempotently and is gated to the confirmed floor year.
  - Verify: run the PBP normalization stage twice; attempt a pre-floor year.
  - Expected: stable row count on re-run (unique on `(nba_stats_game_id, event_num)`); pre-floor years parse zero events and leave `pbp_available=false`.
  - Evidence: DB counts.

- Shot-diet and assisted-FG columns are recomputed by the metrics rebuild and roll up correctly.
  - Verify: run the SL metrics rebuild; inspect `summer_league_player_seasons`.
  - Expected: new columns populate for shot/PBP-eligible player-competitions; additive counts (FGA by zone, assisted FGM) sum across competitions in any career rollup.
  - Evidence: DB spot-check; a player with two same-year venues sums correctly.

- New schema modules are wired for table creation.
  - Verify: fresh integration DB setup.
  - Expected: `summer_league_shot_events` and `summer_league_play_by_play_events` are created — i.e. imported in `tests/integration/conftest.py`; Alembic migration creates/drops them cleanly (`upgrade head` then `downgrade base`).
  - Evidence: migration round-trip output.

---

## Scope, Auth, And Safety

- All new surfaces are public (no auth) and consistent with the aggregator posture.
  - Verify: hit the player and game pages anonymously.
  - Expected: charts render without login; no live/auto-refresh affordances introduced (daily-refresh posture preserved).
  - Evidence: anonymous browser session.

- Out-of-scope Phase 3 stats are absent.
  - Verify: scan game and team-season pages.
  - Expected: NO on/off splits, NO 5-man lineup net ratings surfaced anywhere. (Their absence is intentional — noisy at SL sample sizes.)
  - Evidence: page inspection.

- Empty / negative states are graceful.
  - Verify: a player with no SL shots; a pre-PBP-era game; a game whose shotchart file is missing or failed to parse.
  - Expected: no chart crash; a tasteful "no shot data" / "play-by-play not available for this era" message; the rest of the page renders normally.
  - Evidence: screenshots of each empty state.

---

## Operational Behavior

- The feature consumes already-captured raw JSON without a re-scrape for in-window years.
  - Verify: run shot-event + PBP normalization for 2024 Vegas from existing `data/raw/.../games/{game_id}/shotchartdetail.json` and `playbyplayv2.json`.
  - Expected: parsing succeeds from local raw files; no `stats.nba.com` calls required for parsing.
  - Evidence: run log shows file reads, not network fetches.

- Page query budgets stay within the perf guard.
  - Verify: `make perf` after wiring new reads.
  - Expected: `/players/{slug}/summer-league`, `/players/{slug}`, and `/stats/summer-league/games/{game_id}` stay within (consciously updated) budgets in `tests/integration/perf/budgets.py`.
  - Evidence: `make perf` output.

- New queries are indexed.
  - Verify: `make explain ROUTE=<player|game page>` against the Neon prod-read branch after the migration deploys there.
  - Expected: shot-event and PBP reads use Index Scans (on `(player_id, competition_id)` / `(game_id)`), not Seq Scans on large tables.
  - Evidence: EXPLAIN output. (Cannot run until the new tables exist on the prod-read branch — same caveat as the metrics table.)

---

## Final Browser QA

Run `make dev`; use the **anonymous** recipe from `docs/plans/ai-orchestrator-ticket-spec.md` (these are public pages). Capture screenshots under `tests/visual/screenshots/`.

1. Player with rich SL shot data: zone heat renders, colors read correctly vs. pool, dot toggle works, shot-diet + assisted-FG% present.
2. Player with low sample: chart suppressed, table-only + small-sample note, badges present.
3. Player with no SL shots: graceful empty state.
4. Game box score (PBP era): per-team/per-player shot chart toggle + game-flow chart.
5. Game box score (pre-PBP era): shot chart where available, game-flow gracefully absent.
6. Mobile widths: chart is legible, court not clipped, toggle reachable, respects compact-view conventions.
7. Visual language matches `docs/style_guide.md` (retro analytics palette, Russo One / Azeret Mono, BEM classes).

---

## Completion Bar

The feature is product-complete when QA can demonstrate:
1. A resolved player's SL shot chart renders as zone heat colored vs. the competition pool, with a working raw-dot toggle, on both the player section and per-season page.
2. Shot-diet columns (rim/mid/3/corner-3 rates) and assisted-FG% appear on player SL surfaces and match the underlying parsed rows.
3. The game box-score page shows per-team/per-player shot charts and a game-flow chart for PBP-era games.
4. Parsers are idempotent, resolve players where possible without dropping unresolved shots, and set availability flags from parsed content.
5. Low-sample, empty, and pre-PBP-era states degrade gracefully; no Phase 3 (on/off, lineups) stats are surfaced.
6. `make perf` passes within updated budgets and new queries are index-backed.
</content>
