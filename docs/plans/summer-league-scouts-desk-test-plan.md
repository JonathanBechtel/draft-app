# Summer League Desk — Test Plan

**Sources (source of truth):**
- Behavior spec: `docs/plans/summer-league-scouts-desk-behavior-spec.md`
- Event Desk framework: `docs/plans/event-desk-framework.md`
- Annotated mockup: `mockups/draftguru_sl_scout_desk.html` (review-only controls;
  production renders one state)

**Sibling artifact:** QA checklist at `summer-league-scouts-desk-qa-checklist.md`

> **Regenerated Jul 8** against the 3-state design + Event Desk framework. Out of
> scope (do not test): Desk Wire ticker, Stakes trigger, Second Summer, Roster Wire, Contract
> Watch section, "Priors Updated" echo panel, two-way / contract-outcome data.

## Purpose

Three product risks drive the tests:
1. **State-machine wrongness** — the module is the site's front door and state is derived from
   game status + a schedule-relative flip; rendering the wrong state (or breaking on off-days /
   TZ boundaries / empty schedules / off-window) is the most visible possible failure.
2. **Statistical misrepresentation** — every grade, hero pctl, and sentence is a *computed*
   claim; a wrong claim is worse than no claim. Lock down the cohort-percentile math, the
   storyline rules, and the deterministic fact→phrase pipeline against hand-computed fixtures.
3. **Home-page regression** — `/` has a known query-count problem; the Desk adds data surfaces
   and must read **precomputed projections** (T2/T4/`event_desk_state`), not recompute per request.

Determinism is a first-class requirement: commentary is template-rendered from computed facts
with **no runtime LLM** — the same fixture must render byte-identical copy. Tests assert on the
computed values feeding templates, plus one golden-string test per template family; never scrape
numbers back out of prose.

## Repo conventions (see `docs/plans/ai-orchestrator-ticket-spec.md`)

- Conda env `draftguru`; run via `conda run -n draftguru --no-capture-output python -m pytest <path>`.
- Unit: `tests/unit/` (no DB). Integration: `tests/integration/` (needs `TEST_DATABASE_URL` +
  `PYTEST_ALLOW_DB=1`; wrap shell with `scripts/with-db-env.sh`; run with
  `GEMINI_API_KEY= GEMINI_SUMMARIZATION_API_KEY=` to dodge embedding-listener flakiness).
- New schema modules (`event_desk`, T1–T4) under `app/schemas/` **must** be imported by
  `tests/integration/conftest.py`.
- Perf: `make perf`. Explain: `make explain ROUTE=/` (prod-like Neon branch via
  `EXPLAIN_DATABASE_URL`). Visual: `make visual` → `tests/visual/screenshots/`.
- `/` perf baseline has an open N+1 (52 queries); the Desk must not build on it — set its budget
  entry consciously in `tests/integration/perf/budgets.py`.

## Required Build-Time Tests

| Requirement | Test Type | Suggested Test | Ticket Mapping |
|---|---|---|---|
| Inner state resolver: live (≥1 in_progress, live-always-wins) / ledger (all final) / morning (all upcoming) from (now, schedule, statuses), event-TZ | unit | `tests/unit/test_sl_desk_state.py` — table-driven incl. UTC-vs-PT boundaries | create-project |
| **State resolved at request time** (not read from stored hourly state); **scheduled-tip fallback**: `now ≥ first_tip` + stale statuses ⇒ Live with honest last-tick stamp | unit + integration | `...::test_request_time_resolution_and_tip_fallback` — clock past tip, statuses still SCHEDULED ⇒ Live | create-project |
| **Schedule/scoreboard ingest (Job B step 0)**: upserts today's+tomorrow's games with `tip_datetime` + `IN_PROGRESS`/`FINAL` statuses before normalize | integration | `tests/integration/test_sl_desk_tick.py::test_schedule_ingest` — seeded scoreboard payload → games rows w/ tip times + statuses | create-project |
| Ledger→Morning **schedule-relative flip** at `max(first_tip−6h, 09:00 ET)`; off-day (no tip) never flips → Ledger persists; day rollover | unit | `...::test_ledger_to_morning_flip` — mocked clocks around the boundary + zero-game day | create-project |
| Outer lifecycle phase from calendar+window priors (announce/pre-roll/gap-bridge/post-roll); contiguous window bridges CA→SLC→Vegas gaps | unit | `tests/unit/test_event_lifecycle.py` — Dormant/Announced/Warm-up/Active/Wind-down/Archived cases | create-project |
| Storyline rules: each of Debut/Duel/Streak/Status heat/2nd-look fires on its condition, not on near-misses (**no Stakes**) | unit | `tests/unit/test_sl_desk_storylines.py` — 1 positive + 1 boundary-negative per rule; pure funcs | create-project |
| Storyline weighting `Σ(base×magnitude)`, magnitude = consensus-rank prominence; marquee = top weight; deviation-first (morning expected vs live realized; finals sink) | unit | `...::test_slate_ranking_deterministic` — prominence-differing games; documented weight table | create-project |
| Slot-cohort percentile: lottery ±3 clamped to 1–14 / late R1 15–30 / R2 31–60 / undrafted status, percentile + grade thresholds (hot/warm/mid/cold), gate ladder on thin/1-game sample | unit | `tests/unit/test_sl_desk_cohorts.py` — hand-computed pctl vs output; gated behavior | create-project |
| Debut bar = cohort mean GmSc for slot's first-ever SL games | unit | `...::test_debut_bar` | create-project |
| Cohort membership: Lottery(R1 & ≤14)/R1(1–30)/R2(31–60)/Full/Sophomores/Undrafted(no draft_pick); cap-30 = top-30 by active sort | unit | `tests/unit/test_sl_desk_tracker_cohorts.py` — boundary picks 14/15/30/31, 35-member overflow | create-project |
| Stat-view taxonomy: box family rescales per-36/per-100 (counting stats scale, shooting %s invariant); advanced column set; fixed frame + GmSc sort constant | unit | `tests/unit/test_sl_desk_stat_views.py` — per-36 = per-game×36/MIN; FG% identical across box family | create-project |
| **Commentary pipeline determinism**: fact detectors emit typed facts; notability selection picks highest-extremity angle; template render = golden string; same input ⇒ identical output | unit | `tests/unit/test_sl_desk_commentary.py` — one fixture per fact-kind (cohort_rank/percentile/streak/self_delta/leads_field/debut_vs_bar/count_club/first_since); double-render equality; **no banned copy** | create-project |
| Desk service returns full page payload per state in **one service call, no per-player queries**, reading T2/T4/`event_desk_state` projections | integration | `tests/integration/test_sl_desk_home.py::test_payload_shape_per_state` — seed schedule+logs+baselines, request `/` per mocked state, assert sections present/absent | create-project |
| Job B tick writes T2 grades / T3 storylines / T4 slate / `event_desk_state` from seeded logs + T1 baseline; never rebuilds a distribution | integration | `...::test_desk_tick_projections` | create-project |
| Job A builds T1 baselines (versioned, `is_active` flip) from historical player-seasons + draft slot; all-venue blend + min-minutes gate | integration | `tests/integration/test_sl_cohort_baselines.py` | create-project |
| Tracker aggregates exact vs seeded logs across **all SL games (all venues, not tournament-only)**; toggles filter populations; 0-GP em-dash placeholder; sort | integration | `...::test_tracker_aggregates_and_toggles` | create-project |
| Live tick board Top Performer = max-GmSc tracked player per game; em-dash before tip | integration | `...::test_live_board_top_performer` | create-project |
| Ledger = single full-width table; Performance-of-Night hero ranked by **cohort percentile not raw GmSc** (tie → raw GmSc) | integration | `...::test_ledger_hero_by_percentile` | create-project |
| Freshness stamp equals actual last tick; stale tick never fabricates a time | integration | `...::test_freshness_honesty` | create-project |
| Unresolved players render as text (no broken links); resolved deep-link correctly | integration | `...::test_deep_links_and_unresolved` | create-project |
| Off-window `/` renders collapsed strip only; pre-feature home unchanged; stat pages (Explorer/Leaders) unaffected by window | integration | `...::test_off_window_collapse` | create-project |
| `/` query budget holds in **every** state | integration (perf) | extend `tests/integration/perf/budgets.py` + `make perf` parameterized over seeded states | create-project |
| New Desk queries index-backed (T2 `(competition_id,cohort_key)`, T3/T4 `(game_date,competition_id)`) | integration (explain) | `make explain ROUTE=/` on prod-like branch; capture in PR | create-project (pre-merge gate) |

## Required Post-Build QA

| Requirement | Verification Path | Evidence |
|---|---|---|
| Each state renders on the live dev app (morning / live / ledger / off-window via seeded schedule or state override) | e2e / browser (Playwright MCP, no login) | `tests/visual/screenshots/sl-desk-{morning,live,ledger,offwindow}.png` |
| Module renders the single current state per the state machine; **no user-facing state switcher** in the DOM | e2e / browser | per-state screenshot + DOM assertion (no preview controls) |
| Hero per state (marquee face-off / live duel / performance-of-night); no competitive/ticker/scout copy | e2e / browser + text audit | screenshots + grep of rendered page for banned terms |
| Live board Top Performer + grades match DB spot-checks | e2e / browser + SQL | annotated screenshot + query output in ticket |
| Tracker cohort toggles + stat-view (Box/Per-36/Per-100/Advanced) + column sort work in-page; rows deep-link | e2e / browser | `sl-desk-tracker.png` + click-through |
| JS-disabled cold load renders all content server-side | e2e / browser | JS-off screenshot |
| Mobile (390px): no horizontal page scroll; tables scroll in-container; hero stacks compactly enough that the next section is discoverable | e2e / browser | `sl-desk-mobile.png` |
| Percentile/grade chips legible vs style guide (contrast, heat direction consistent with SL heat-shading); spacing below hero | visual | `make visual` diff review |
