# Summer League Desk #548 — Deferred Perf/Plan Verification Handoff

## Status

Implementation and the **≤5-Desk-query homepage contract** are DONE and verified in this
environment (`make perf` green, see below). The **query PLAN** (Index Scan vs Seq Scan) and
the **sub-500ms cold/warm latency** target from ticket #548 are **NOT verified** — this
environment cannot produce a faithful answer for either, per the ticket's own checkpoint:

- `EXPLAIN_DATABASE_URL` here points at **prod-write**, not a prod-like **read** branch.
  Running `EXPLAIN ANALYZE` against prod-write is explicitly disallowed by the ticket.
- The dev Neon volume Seq-Scans routinely even when a correct index exists (too little data,
  planner prefers a scan over a tiny table) — a "Seq Scan" observed here would not be
  evidence of a missing index, and an "Index Scan" observed here would not be strong
  evidence either. Neither reading is trustworthy from this box.
- Wall-clock latency against dev is not representative of Neon prod network/latency
  characteristics, so no cold/warm timing number measured here would mean anything for the
  500ms target.

**A human with access to a production-like Neon READ branch must run the commands in
"What the human needs to run" below and record the result.** Do not treat this document, or
the `status:done` label, as a claim that the plan/latency check passed — it is explicitly
deferred.

## What IS verified here (query count — fully checkable without prod data)

`make perf` (`tests/integration/perf/`) is green — 26 passed. Specifically:

- `tests/integration/perf/test_desk_state_resolution_budget.py` (NEW): asserts
  `get_desk_view_from_snapshot`'s own query count is `<= 5` for all five states the ticket
  names — **Off-window** (both the no-`events`-row and the dormant-with-an-existing-row
  shapes), **Preview**, **Live**, **Recap**, and **Wind-down**. Measured: 1 (off-window, no
  row) / 3 (off-window, dormant with row) / 4 (preview/live/recap/wind-down, in-window).
- `tests/integration/perf/test_desk_home_inwindow_budget.py`: the WHOLE `/` route, in-window,
  is `55` queries for both Live and Recap (down from `57` pre-#548, `71` pre-#551) —
  `tests/integration/perf/budgets.py::DESK_HOME_PAGE_BUDGETS`.
- `tests/integration/perf/test_desk_tick_query_growth.py` (NEW): a 20-player tick issues the
  same query count as a 2-player tick (measured delta: 0; asserted bound: `<= 3`), proving
  the tick's grading/storyline/commentary steps no longer grow with roster size.
- `tests/integration/perf/test_route_query_budgets.py`: `ROUTE_BUDGETS["/"] = 52` (off-window,
  unaffected by this ticket's changes) still holds.

None of this requires prod data — it is a statement-COUNT guard (see
`tests/integration/perf/_capture.py`), deterministic against the seeded test dataset
regardless of row volume.

## What the human needs to run

### 1. Cold/warm latency against a Neon prod-like READ branch

```bash
# Point EXPLAIN_DATABASE_URL at a Neon READ branch (NOT prod-write) in .env, then:
scripts/with-db-env.sh conda run -n draftguru python scripts/explain_route.py / --no-plans
```

Run it twice in a row (first = cold, second = warm-cache) during a window when the Desk is
in-window (e.g. during Vegas 2026, Jul 9-19) so the timing reflects the real in-window path,
not the off-window short-circuit. Confirm both numbers are under 500ms. If `/` is off-window
when you run this, either force it via `settings.sl_desk_force_mode="on"` +
`sl_desk_force_date` (see `app/config.py`) pointed at an in-window date with real seeded
data on that branch, or wait for the live window.

### 2. Query plans for every Desk query (Index Scan, not Seq Scan on a large table)

```bash
scripts/with-db-env.sh conda run -n draftguru python scripts/explain_route.py / --top 10
```

Run this against the SAME Neon read branch, in-window. For each Desk-attributable query in
the report, confirm it shows an **Index Scan** (or **Index Only Scan** / **Bitmap Index
Scan**), not a **Seq Scan** on `summer_league_games`, `summer_league_player_seasons`,
`summer_league_desk_player_grades`, `summer_league_desk_slate`, or `events` (the tables large
enough for a Seq Scan to matter). The specific access-path claim per query:

| Query (function) | Filter columns | Expected access path | Backing index |
|---|---|---|---|
| `events` lookup (`desk_read._resolve_window_state`) | `key` | Index Scan | `uq_events_key` (unique constraint) |
| `calendar_facts_for_competition_ids` — game dates (`registry.py`) | `competition_id IN (...)`, `game_date IS NOT NULL` | Index Scan | `ix_summer_league_games_competition_date` (competition_id, game_date) |
| `calendar_facts_for_competition_ids` — today's schedule/status (`registry.py`) | `competition_id IN (...)`, `game_date = :today` | Index Scan | `ix_summer_league_games_competition_date` |
| `get_render_snapshot` (`event_desk.render_snapshots`) | `event_id`, `daily_state`, `tracker_cohort`, `tracker_stat_view` | Index Scan | the render-snapshot table's composite unique constraint (see `app/schemas/event_desk_render_snapshot.py`) |
| `grade_players_bulk` — competition fetch | `id` (PK) | Index Scan | primary key |
| `grade_players_bulk` — players fetch | `PlayerMaster.id IN (...)` | Index Scan | primary key |
| `grade_players_bulk` — season rows | `player_id IN (...)`, `year = :year` | Index Scan | `ix_summer_league_player_seasons_player_id` |
| `grade_players_bulk` — baseline fetch | `baseline_version`, `cohort_key IN (...)`, `is_active` | Index Scan | `uq_summer_league_cohort_baselines_version_cohort` and/or `ix_summer_league_cohort_baselines_cohort_active` |
| `grade_players_bulk` — bulk upsert | `player_id, competition_id, baseline_version` (ON CONFLICT) | Index Scan (upsert path) | `uq_summer_league_desk_player_grades_player_competition_version` |
| `compute_desk_storylines` — batched `fetch_game_lines`/`fetch_prior_events`/`fetch_current_event_gp` (`desk_fact_queries.py`) | `player_id IN (...)`, `competition_id`/`year` | Index Scan | `ix_summer_league_player_seasons_player_id`, join on `summer_league_player_game_logs`'s existing player/competition indexes |
| `persist_grade_facts_bulk` — select | `player_id IN (...)`, `competition_id`, `baseline_version` | Index Scan | `ix_summer_league_desk_player_grades_player_id` / the unique constraint above |
| `persist_grade_facts_bulk` / `persist_slate_facts_bulk` — bulk `UPDATE ... WHERE id = :id` | `id` (PK) | Index Scan | primary key |
| `persist_slate_facts_bulk` — select | `game_id IN (...)` | Index Scan | `uq_summer_league_desk_slate_game` |

**None of these are new query shapes.** Every one filters on the exact same columns the
pre-#548 per-player/per-game equivalents already filtered on (only the predicate changed from
`==` to `.in_(...)`, which uses the same B-tree index) — confirmed by schema inspection in
this session (see `app/schemas/summer_league_metrics.py`, `app/schemas/summer_league.py`,
`app/schemas/summer_league_desk.py`). **No new index or migration was added in this change**
because none was needed; the human verification above is to catch anything this session's
column-by-column schema read missed, not because a gap is expected.

## Why this is deferred, not skipped

Per the ticket's CRITICAL checkpoint: this agent does not have write access to a Neon
prod-like read branch, and running `EXPLAIN`/timing against prod-write or the dev volume
would produce numbers that are actively misleading (a false pass or false fail either way).
The query-COUNT contract above is the part of the DoD that is meaningfully verifiable without
that access, and it is fully green. The label `status:done` on issue #548 reflects that the
implementation and the verifiable contract are complete — it does NOT mean the plan/latency
check passed, because it has not been run.
