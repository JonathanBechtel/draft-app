# Summer League PBP & Shot Charts Test Plan

**Sources:**
- Tech spec: `docs/plans/summer-league-pbp-shotchart-plan.md`

**Sibling artifact:** QA checklist at `summer-league-pbp-shotchart-qa-checklist.md`

## Purpose

Tie tests to the real product risks of this feature: (1) **silent data corruption** — a shot/PBP parser that drops, duplicates, or mis-resolves rows poisons every downstream stat invisibly; (2) **misleading small-sample signal** — SL prospects play 3–5 games, so charts and rates must suppress/badge low samples; (3) **calibration correctness** — zone coloring and composites are pool-relative; a wrong baseline lies confidently; (4) **scope creep** — Phase 3 (on/off, lineups) must not leak into the UI; (5) **perf regressions** — new per-shot/per-event reads on hot player/game pages.

Repo conventions: `tests/unit/` (no DB), `tests/integration/` (DB + FastAPI via HTTPX, needs `TEST_DATABASE_URL` + `PYTEST_ALLOW_DB=1`), `tests/visual/` (`make visual`), perf guard `make perf` / `tests/integration/perf/budgets.py`. Run via `conda run -n draftguru ...`. No `no_deps`/`with_deps` split. Disable Gemini keys for integration runs (`GEMINI_API_KEY= GEMINI_SUMMARIZATION_API_KEY=`).

## Required Build-Time Tests

| Requirement | Test Type | Suggested Test | Ticket Mapping |
|---|---|---|---|
| `shotchartdetail` JSON → shot-event rows: correct field mapping (loc_x/y, zone, distance, made, period/clock) | unit | `tests/unit/services/summer_league/test_shotchart_parser.py` — feed a fixture payload, assert parsed dataclass rows | create-project (T1.2) |
| Shot-event upsert is idempotent | integration | `tests/integration/services/test_shotchart_ingest.py` — parse a slice twice, assert stable count + unique `(nba_stats_game_id, game_event_id)` | create-project (T1.2) |
| Unresolved shots persisted with `player_id IS NULL`, not dropped | integration | same module — fixture with an unresolved source player; assert row exists, null player_id | create-project (T1.2) |
| `shotchart_available` set from parsed rows, not file presence (empty file ⇒ false) | integration | `tests/integration/services/test_sl_normalization_flags.py` | create-project (T1.2) |
| Zone aggregation: per-zone FGA/FGM/FG%/freq% correct; pool league-average baseline correct | unit | `tests/unit/services/test_shotchart_service_zones.py` — synthetic shots, assert zone math + pool avg | create-project (T1.3) |
| Minimum-attempts floor suppresses the chart (≥20 FGA) | unit | same module — below-floor input returns "table-only/suppressed" signal | create-project (T1.3) |
| Shot-dot read returns raw (x,y,made) for a player-competition | unit/integration | `test_shotchart_service_zones.py` / ingest test | create-project (T1.3) |
| Shot-diet columns (rim/mid/3/corner-3 rate) computed in metrics rebuild; additive roll-up across same-year venues | integration | `tests/integration/services/test_sl_metrics_shotdiet.py` — rebuild, assert columns + career sum | create-project (T1.4) |
| `SummerLeagueShotEvent` schema: migration up/down round-trip; conftest import present | integration | migration test + `tests/integration/conftest.py` import; `alembic upgrade head`/`downgrade base` | create-project (T1.1) |
| Player SL route exposes shot-chart + shot-diet context; resolves only for resolved players | integration | `tests/integration/routes/test_player_sl_shotchart.py` — HTTPX GET `/players/{slug}` + `/players/{slug}/summer-league/{year}`, assert context keys & values | create-project (T1.6) |
| Game box-score route exposes per-team/per-player shot-chart context | integration | `tests/integration/routes/test_game_box_shotchart.py` — GET `/stats/summer-league/games/{game_id}` | create-project (T1.7) |
| Zone-heat + dot-toggle renders; toggle works without reload | visual + e2e | `make visual` capture of player page; Playwright MCP toggle interaction | create-project (T1.5/T1.6) |
| Player shot-chart share card renders with correct stat line | visual | extend share-card capture | create-project (T1.8) |
| `playbyplayv2` JSON → PBP-event rows: field mapping (period/clock, score margin, actors, event type) | unit | `tests/unit/services/summer_league/test_pbp_parser.py` | create-project (T2.2) |
| PBP-event upsert idempotent + floor-gated (pre-floor year ⇒ 0 events, flag false) | integration | `tests/integration/services/test_pbp_ingest.py` | create-project (T2.2) |
| Game-flow series: score-margin-over-time endpoints match final score; monotonic time | unit | `tests/unit/services/test_game_flow.py` — synthetic PBP, assert series | create-project (T2.3) |
| Assisted-FG%: `ast_fgm/(ast_fgm+unast_fgm)` from PBP made-FG events; stored on player-season | unit + integration | `tests/unit/.../test_assisted_fg.py` (math) + metrics rebuild integration | create-project (T2.4) |
| Game-flow chart renders on box-score page (PBP era) | visual | `make visual` / Playwright capture | create-project (T2.3) |
| New page queries within perf budget | integration (perf) | `make perf`; update `tests/integration/perf/budgets.py` for player/game routes | create-project (T1.6/T1.7/T2.3) |

## Required Post-Build QA

| Requirement | Verification Path | Evidence |
|---|---|---|
| Zone colors read correctly vs. pool on a real rich-sample player | browser (anon) | screenshot `tests/visual/screenshots/sl-shotchart-rich.png` |
| Low-sample player: chart suppressed, table + small-sample note, badges | browser (anon) | screenshot `sl-shotchart-lowsample.png` |
| No-SL-shots player: graceful empty state | browser (anon) | screenshot `sl-shotchart-empty.png` |
| Game box score (PBP era): shot-chart toggle + game-flow chart | browser (anon) | screenshots `sl-game-shotchart.png`, `sl-game-flow.png` |
| Pre-PBP-era game: shot chart where present, game-flow absent gracefully | browser (anon) | screenshot `sl-game-prepbp.png` |
| Mobile legibility (court not clipped, toggle reachable) | browser (anon, mobile viewport) | screenshot `sl-shotchart-mobile.png` |
| No Phase 3 (on/off, lineup) stats anywhere | browser (anon) | page inspection note |
| New queries index-backed | `make explain ROUTE=...` on Neon prod-read branch post-deploy | EXPLAIN output |
| Parser runs from existing raw files, no re-scrape | run normalization on 2024 Vegas locally | run log |

Browser steps use the **anonymous** recipe (public pages) from `docs/plans/ai-orchestrator-ticket-spec.md`; no admin login needed.

## Ticket Injection Notes

- **Ticket: Phase 0 — pin PBP floor + tighten shotchart params** (T0.1)
  - Required: drill `playbyplayv2` for a 2017 and 2018 Vegas (L15) game; record the real PBP floor in the spec/coverage table.
  - Required: confirm `build_shotchart_params` (`app/services/summer_league/endpoints.py`) constrains to `GameID` + `ContextMeasure=FGA`; add/adjust a unit test asserting the param dict.
  - Required: confirm `curl_cffi` is a first-class dependency in `pyproject` extras.
  - No schema/UI; no DB tests.

- **Ticket: SummerLeagueShotEvent schema + migration** (T1.1)
  - Files: `app/schemas/summer_league.py` (new table), Alembic migration (`create_all`/`drop_all` for the new table), `tests/integration/conftest.py` (import).
  - Required tests: migration up/down round-trip; table present after conftest setup; unique `(nba_stats_game_id, nba_stats_game_event_id)`; indexes `(player_id, competition_id)`, `(game_id)`.

- **Ticket: Shot-event parser in normalization** (T1.2)
  - Files: `app/services/summer_league/normalization.py`, raw-store reads.
  - Required tests: field-mapping unit test from a `shotchartdetail` fixture; idempotent re-parse; unresolved shot stored null-player; `shotchart_available` + `summer_league_raw_files.parse_status` set from parsed content (empty file ⇒ flag false).

- **Ticket: summer_league_shotchart_service.py** (T1.3)
  - Files: new service module.
  - Required tests: zone aggregation math; per-pool league-average baseline; ≥20-FGA suppression signal; raw-dot read.

- **Ticket: Shot-diet columns into metrics rebuild** (T1.4)
  - Files: `app/services/summer_league/metrics.py`, `app/schemas/summer_league_metrics.py` (new cols on `SummerLeaguePlayerSeason`), migration (`op.add_column`, idempotent `IF NOT EXISTS`).
  - Required tests: columns populate on rebuild; additive roll-up across same-year venues sums correctly.

- **Ticket: Zone-heat + dot-toggle SVG component** (T1.5)
  - Files: `app/static/summer-league-shotchart.js` + CSS; data via `window.SL_SHOTCHART`.
  - Required: visual capture; Playwright toggle interaction (no reload); style-guide compliance (BEM, palette/fonts).

- **Ticket: Player SL shot-chart wiring** (T1.6)
  - Files: `app/routes/summer_league.py` (+ ui.py if player page lives there), `app/services/summer_league_stats_service.py` context, template.
  - Required tests: route context integration test; perf budget bump for `/players/{slug}` and `/players/{slug}/summer-league`.

- **Ticket: Game box-score shot-chart wiring** (T1.7)
  - Files: `app/routes/summer_league.py`, `app/services/summer_league_games_service.py`, box-score template.
  - Required tests: route context integration test; perf budget for `/stats/summer-league/games/{game_id}`.

- **Ticket: Player shot-chart share card** (T1.8)
  - Files: share-card spec/template per existing pattern.
  - Required: visual capture with correct stat line.

- **Ticket: SummerLeaguePlayByPlayEvent schema + migration** (T2.1)
  - Files: `app/schemas/summer_league.py`, migration, conftest import.
  - Required tests: round-trip; unique `(nba_stats_game_id, event_num)`; index `(game_id, period, event_num)`.

- **Ticket: PBP parser (floor-gated)** (T2.2)
  - Files: `app/services/summer_league/normalization.py`.
  - Required tests: field-mapping unit test; idempotent re-parse; pre-floor year ⇒ 0 events + `pbp_available=false`.

- **Ticket: Game-flow chart** (T2.3)
  - Files: service (series builder), box-score template/JS.
  - Required tests: series-math unit test (endpoints = final score); visual capture; perf budget.

- **Ticket: Assisted-FG% derivation + surface** (T2.4)
  - Files: `metrics.py` (+ `SummerLeaguePlayerSeason` cols + migration), player template.
  - Required tests: assisted-FG math unit test; metrics rebuild integration assertion; player-page context test.

## Notes for create-project

- Dependency order within Phase 1: T1.1 → T1.2 → {T1.3, T1.4} → {T1.5} → {T1.6, T1.7} → T1.8. Phase 2 mirrors: T2.1 → T2.2 → {T2.3, T2.4}. T0.1 gates all PBP-era assertions (it fixes the floor year used in tests/fixtures).
- Every ticket ends on the Definition of Done in `CLAUDE.md`: `make precommit`, full `mypy app --ignore-missing-imports`, `pytest tests/unit` (+ `tests/integration` for DB/route work), `make coverage.diff` ≥80% patch, `make visual` for UI, `make perf` + `make explain` for new plumbing.
- UI tickets get integration + e2e (Playwright MCP) + visual; schema/parser/service tickets get unit + integration.
</content>
