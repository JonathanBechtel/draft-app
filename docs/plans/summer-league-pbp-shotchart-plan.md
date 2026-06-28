# Summer League — Play-by-Play & Shot-Chart Incorporation Plan

**Status:** Planning (Jun 27 2026)
**Decision owner:** Jonathan
**Scope chosen:** Shot charts first + lean PBP next (on/off & lineups deferred). Shot charts render as **zone heat with a raw-dot toggle**.

Companion docs: `docs/plans/summer-league-stats-pages.md` (page inventory), `docs/summer_league_api_probe_findings.md` (PBP/shotchart availability), `docs/summer_league_raw_ingestion.md` (raw fetch pipeline), `docs/summer_league_advanced_metrics_methodology.md`.

Related memory: `project_sl_pages_sequencing` (PBP tier deferred), `project_sl_advanced_metrics_wiring`, `project_summer_league_dev_data_state`, `project_nba_stats_api_blocked`.

---

## 1. The framing that changes everything

The expensive, genuinely-hard half of this feature is **already built and running**:

- `scripts/fetch_summer_league_raw.py` already fetches **`playbyplayv2.json` and `shotchartdetail.json` for every game** and archives them to S3 (`data/raw/nba_stats/summer_league/{year}/{league_id}/games/{game_id}/`).
- The TLS-impersonation obstacle (`stats.nba.com` Akamai fingerprint block) is solved via `curl_cffi` in `NBAStatsClient`.
- The schema already carries the anchors: `SummerLeagueGame.nba_stats_game_id`, `SummerLeaguePlayerGameLog.nba_stats_person_id`, `SummerLeagueSourcePlayer.nba_stats_person_id`, plus `SummerLeagueCompetition.pbp_available` / `.shotchart_available` flags.
- Raw-file presence is already audited into `SummerLeagueRawFile` (with `endpoint`, `game_id`, `parse_status`).

**What is missing is purely downstream:** parse raw JSON → tables → derive stats → render. Today `normalization.py` only *checks that the PBP/shotchart files exist* to set a data-quality flag (`normalization.py:576–611`); it never reads their contents. So this is a "build the consumer half" project, not a "go acquire data" project.

---

## 2. Two sources, two cost/value/coverage profiles

Treat these as separate pipelines — they differ in difficulty, signal, and historical reach.

| Source | Unlocks | Parse difficulty | Coverage |
|---|---|---|---|
| **`shotchartdetail`** | Shot charts, shot-zone FG%, shot diet (rim/mid/3 rates), shot-creation profile | **Low** — one clean row per shot, no state machine | Likely 2010+ (keyed on season; broader than PBP) |
| **`playbyplayv2`** | Game-flow chart, true-possession pace, assisted-FG% | Low | 2019+ confirmed; 2017–18 unverified |
| **`playbyplayv2`** (heavy) | On/off splits, 5-man lineup net ratings | **High** — substitution state machine | 2019+ only |

### Sample-size reality (drives what we surface)
A prospect plays ~3–5 SL games. **On/off splits and lineup net ratings are essentially noise at that sample** — the flashiest PBP feature is the *least* defensible for a draft-analytics product, which is why it's deferred. Shot diet and shot charts aggregate 30–50 FGA across a run: borderline but defensible, and squarely on-brand for the scouting lens. Every rate stat keeps the existing sample-size badge discipline (`5 GP · 87 MIN` pills, rate-stat italics under the minute floor).

---

## 3. Phase 0 — Prerequisites (small, gating)

1. **Pin the PBP floor.** Probe only *confirmed* 2019+; the working assumption is ~2017. Drill `playbyplayv2` for one Vegas (L15) game in 2017 and 2018 (extend `scripts/probe_summer_league_api.py` or a one-off). Record the real floor; it gates which years get game-flow / assisted-FG%.
2. **Tighten `shotchartdetail` params.** The probe noted it leaks season-scoped rows (returned data even where box was empty) because it keys off Season. Confirm `build_shotchart_params` constrains to `GameID` + `ContextMeasure=FGA` so per-game parsing is exact. (`app/services/summer_league/endpoints.py:155`.)
3. **Confirm `curl_cffi` is a first-class dependency** (not probe-only) in `pyproject` extras — the ingestion runbook already installs it via `pip install -e ".[dev]"`, so likely done; verify.
4. **Confirm raw coverage** for target years exists locally / in S3 for the clean window (2021–2025 + 2017–2019) so the parser has inputs without a re-scrape. If gaps, re-run `fetch_summer_league_raw.py` for missing slices.

No schema or UI work in Phase 0.

---

## 4. Phase 1 — Shot charts + shot profile (the headline)

The highest value/effort ratio and the most shareable surface.

### 4.1 Schema — `SummerLeagueShotEvent` (new table, `app/schemas/summer_league.py`)
One row per shot attempt. Fields (mapped from `shotchartdetail`):

- IDs / FKs: `id` (PK), `game_id` → `SummerLeagueGame`, `competition_id`, `team_entry_id`, `source_player_id`, `player_id` (resolved `PlayerMaster`, nullable — ~40% unresolved by design), `nba_stats_person_id`, `nba_stats_game_event_id` (idempotency key with game_id).
- Shot facts: `period`, `minutes_remaining`, `seconds_remaining`, `loc_x`, `loc_y`, `shot_distance`, `shot_type` (2PT/3PT), `shot_zone_basic`, `shot_zone_area`, `shot_zone_range`, `action_type`, `made` (bool).
- Indexes: `(player_id, competition_id)`, `(game_id)`, unique `(nba_stats_game_id, nba_stats_game_event_id)` for idempotent re-pulls.

Migration via Alembic `create_all` (new-table pattern per CLAUDE.md). Add the schema import to `tests/integration/conftest.py`.

### 4.2 Parser — extend `normalization.py`
New stage `normalize_shot_events(...)`, mirroring the box-log parser:
- Read `games/{game_id}/shotchartdetail.json` from `raw_store`.
- Resolve players via the shared `nba_stats_person_id` → `SummerLeagueSourcePlayer` path (reuse existing resolution; unresolved shots still stored with `player_id=NULL`).
- Idempotent upsert keyed on `(game_id, game_event_id)`.
- Update `SummerLeagueRawFile.parse_status` for the `shotchartdetail` endpoint (currently presence-only).
- Set `SummerLeagueCompetition.shotchart_available` from *actual parsed rows*, not just file presence.

### 4.3 Aggregation service — `summer_league_shotchart_service.py` (new)
- `get_player_shot_zones(player_id, competition_id|career)` → per-zone {FGA, FGM, FG%, freq%} for the 7 canonical zones (restricted area / paint non-RA / mid-range / corner-3 L+R / above-break-3 / [optional] backcourt-excluded), plus league-average FG% per zone for the same pool (the color reference).
- `get_player_shot_dots(player_id, competition_id)` → raw (loc_x, loc_y, made) list for the dot-toggle overlay.
- `get_game_shot_zones(game_id, team_entry_id|player_id)` → game-scoped variant for the box-score page.
- Pool league averages: compute per-competition (same recalibration philosophy as composites — `feedback_compute_metrics_bottom_up`), cached.

### 4.4 Shot-diet analytical columns (the layer, not just the chart)
Derive and store on the materialized `SummerLeaguePlayerSeason` (extend `summer_league/metrics.py` `rebuild()`):
- `rim_rate` (% of FGA at the rim), `mid_rate`, `three_rate` (3PAr already exists as `fg3ar` — reconcile), `corner3_rate`.
- These roll up cleanly (additive shot counts), so they feed leaders/explorer too, and enable cohort comparison ("shot diet vs. 2026 class").

### 4.5 Rendering — zone heat + dot toggle (vanilla SVG, no build step)
- New `summer-league-shotchart.js` + CSS, page-scoped, init on `DOMContentLoaded`, data injected via `window.SL_SHOTCHART`.
- **Default:** zone-heat half-court SVG — each zone filled by FG% vs. pool average (canonical green→red), zone label shows `FG% (FGA)`. Legible at 30–50 FGA where dots look empty.
- **Toggle:** overlay raw shot dots (green make / hollow miss) on the same court.
- Sample-size badge + "calibrated to this SL pool" caveat chip near the chart.

### 4.6 Surfaces (Phase 1)
- **Player SL section** (`/players/{slug}` + `/players/{slug}/summer-league/{year}`): zone chart + shooting-by-zone table + shot-diet row. (Page 16/17 in the inventory — extends existing components.)
- **Game box score** (`/stats/summer-league/games/{game_id}`): per-team / per-player shot-chart toggle (inventory page 9, "Shot chart (Tier 2+)").
- **Share card:** new "Player SL Shot Chart" PNG (fits the existing share-card pattern, page 18).

### 4.7 Perf
- Add budgets for the two new reads (shot-zone aggregate + dots) on player/game routes in `tests/integration/perf/budgets.py`; bump the route budgets consciously.
- `make explain` each new query against the Neon prod-read branch *after* the migration deploys there (table won't exist on the branch pre-deploy — same caveat as the metrics table).

---

## 5. Phase 2 — Lean PBP-derived stats

Cheap, high-signal, no state machine.

### 5.1 Schema — `SummerLeaguePlayByPlayEvent` (new table)
One row per PBP event: `game_id`, `competition_id`, `period`, `clock`, `event_type`/`event_msg_type`, `home_score`, `away_score`, `score_margin`, actor person-ids (`person1/2/3` → player_1/2/3 resolved), `description`. Indexed `(game_id, period, event_num)`, unique `(nba_stats_game_id, event_num)`.

### 5.2 Parser
`normalize_pbp_events(...)` in `normalization.py`, mirroring 4.2. Gate to the confirmed PBP floor (Phase 0). Flip `pbp_available` from parsed rows.

### 5.3 Derived stats
- **Game-flow chart** (box-score page): score-margin-over-time line from `score_margin`/clock. Pure read + SVG line; visually strong, near-zero methodology risk.
- **Assisted-FG%** (self-creation proxy): from made-FG events that carry an assister (`person2`). Store `ast_fgm` / `unast_fgm` on `SummerLeaguePlayerSeason`; surface "% of made FGs assisted" on the player page — a genuine scouting signal (creators vs. finishers).
- **True-possession pace reconciliation** (optional): derive possessions from PBP and compare to NBA-provided `pace` (already present box-side 2017+). Low marginal value; do only if cheap.

### 5.4 Surfaces (Phase 2)
- Game-flow chart on the game box-score page.
- Assisted-FG% on the player SL section + per-season page.

---

## 6. Phase 3 — Deferred (on/off + lineups)

Out of scope for this build. Requires a substitution state machine (parse SUB events → maintain on-court 5-man state across each game) to compute lineup minutes, 5-man net ratings, and on/off splits. **Noisiest data at SL sample sizes** — if ever built, surface season-aggregate only with loud sample-size warnings, never per-game. Revisit only on explicit demand. Inventory pages that depend on this: game-page "Lineup combinations" / "On/Off splits", team-season "Notable lineups".

---

## 7. Methodology / open questions to settle in the spec

- **Zone taxonomy:** adopt NBA `SHOT_ZONE_BASIC` (7 zones) directly, or a simpler DraftGuru 4-zone (rim / paint / mid / three)? Recommend mapping NBA zones → a 5–7 zone display, store the raw NBA zone fields so we can re-bucket later.
- **League-average reference for coloring:** per-competition pool average (recommended, consistent with composite recalibration) vs. all-SL vs. NBA. Per-pool keeps "vs. this summer's field" honest.
- **Minimum-attempts floor** for showing a zone chart (avoid a 4-shot chart reading as signal). Propose ≥20 FGA for the season chart; below that show the table only with a "small sample" note.
- **Shot-diet cohort frame:** same draft class (consistent with the M2 cohort question, still open). Shot-diet rates don't need the M1 composite, so they can ship ahead of the moat pages.
- **Resolution gap:** ~40% of SL logs are unresolved players; shot rows inherit that. Charts only render on resolved players' pages; unresolved shots still stored for later backfill.

---

## 8. Suggested ticketing (after `/create-qa-checklist` → `/create-project`)

**Phase 0**
- T0.1 Pin PBP floor (drill 2017/18) + tighten shotchart params; document.

**Phase 1 (shot charts)**
- T1.1 `SummerLeagueShotEvent` schema + migration + conftest import.
- T1.2 Shot-event parser in `normalization.py` + parse-status + availability flag from parsed rows.
- T1.3 `summer_league_shotchart_service.py` (zones, dots, pool averages) + unit tests.
- T1.4 Shot-diet columns into `metrics.py` rebuild + `SummerLeaguePlayerSeason` schema cols.
- T1.5 Zone-heat + dot-toggle SVG component (JS/CSS).
- T1.6 Player SL section + per-season page wiring + perf budget + explain.
- T1.7 Game box-score shot-chart wiring + perf budget.
- T1.8 Player shot-chart share card.

**Phase 2 (lean PBP)**
- T2.1 `SummerLeaguePlayByPlayEvent` schema + migration.
- T2.2 PBP parser (floor-gated) + availability flag.
- T2.3 Game-flow chart on box-score page.
- T2.4 Assisted-FG% derivation + player-page surface.

Each ticket ends on the Definition of Done (`make precommit`, full `mypy app`, unit+integration, `make coverage.diff` ≥80%, `make visual` for UI, `make perf`/`make explain` for new plumbing).

---

## 9. Why this ordering

- Shot charts reuse a clean single-table parse with no state machine, have the broadest historical coverage, are the most visually shareable, and produce the shot-diet analytical layer that fits DraftGuru's scouting voice — all before touching the harder event stream.
- Lean PBP (game flow, assisted-FG%) adds high-signal, low-risk derivations on top.
- The noisy, heavy lineup/on-off tier is explicitly parked so it never blocks the shippable value.
</content>
</invoke>
