# Summer League Scout's Desk — Test Plan

**Sources:**
- Product pitch: `docs/plans/summer-league-scouts-desk-pitch.md`
- Annotated mockup (layout + selection rules): `mockups/draftguru_sl_scout_desk.html`

**Sibling artifact:** QA checklist at `summer-league-scouts-desk-qa-checklist.md`

## Purpose

The product risk is threefold: (1) **state-machine wrongness** — the module is
time-aware, and rendering the wrong state (or breaking on off days / timezone
boundaries / empty schedules) is the most visible possible failure on the site's
front door; (2) **statistical misrepresentation** — every chip, echo, and grade is
a computed claim ("96th pctl vs top-5 cohort", "5 of the last 6 signed deals"), and
a wrong claim is worse than no claim; (3) **home-page regression** — `/` already has
a known query-count problem, and this module adds several data surfaces to it.
Tests must therefore lock down (a) the schedule→state resolver under mocked clocks,
(b) the storyline rule library and every selection rule as pure functions over
fixtures, (c) the cohort/percentile/delta math against hand-computed values, and
(d) the `/` query budget in **every** state. Browser tests confirm each state
actually renders, deep-links work, and JS-off/mobile degrade cleanly.

Design constraint carried from the pitch: all sentences are template-rendered from
computed comparisons. Tests should assert on the computed values feeding templates,
and one integration test per template family should assert the rendered string —
never the reverse (no scraping numbers back out of prose across the suite).

Repo conventions (see `docs/plans/ai-orchestrator-ticket-spec.md`):
- Conda env `draftguru`; tests via `conda run -n draftguru --no-capture-output python -m pytest <path>`.
- Unit tests: `tests/unit/` (no DB). Integration: `tests/integration/` (needs `TEST_DATABASE_URL` + `PYTEST_ALLOW_DB=1`; wrap shell with `scripts/with-db-env.sh`).
- Run integration with `GEMINI_API_KEY= GEMINI_SUMMARIZATION_API_KEY=` to dodge the embedding-listener flakiness.
- Perf: `make perf`. Explain: `make explain ROUTE=...` (prod-like Neon branch via `EXPLAIN_DATABASE_URL`). Visual: `make visual` → `tests/visual/screenshots/`.
- New DB tables/columns (if any — e.g., precomputed desk payload or baselines cache) must be added to `tests/integration/conftest.py` schema imports.
- Home-page perf baseline: `/` currently has an open N+1 follow-up (52 queries). The Desk must not be built on top of that pattern — its budget entry is set consciously in `tests/integration/perf/budgets.py`.

## Required Build-Time Tests

| Requirement | Test Type | Suggested Test | Ticket Mapping |
|---|---|---|---|
| Schedule→state resolver: morning / live / ledger / off-day / off-window from (now, schedule, window config), computed in event TZ | unit | `tests/unit/test_sl_desk_state.py` — table-driven cases incl. UTC-vs-PT boundary times, zero-game day mid-window, day rollover after last final | create-project |
| Storyline rules: each of Debut/Duel/Stakes/Streak/Contract/2nd-look fires on its condition and not on near-misses | unit | `tests/unit/test_sl_desk_storylines.py` — one positive + one boundary-negative fixture per rule; pure functions, no DB | create-project |
| Storyline weighting + marquee selection deterministic; ties broken by headliner consensus rank | unit | `...::test_slate_ranking_deterministic` — same input ⇒ same order; documented weight table from spec | create-project |
| Slot-cohort percentile: window rule (±3 lottery / round buckets), percentile math, sub-floor cohort ⇒ no percentile | unit | `tests/unit/test_sl_desk_cohorts.py` — hand-computed percentiles vs function output; floor behavior | create-project |
| Second Summer math: YoY deltas vs own prior SL; typical-yr-2-jump baseline from returner pairs; no-prior-SL returner excluded | unit | `tests/unit/test_sl_desk_second_summer.py` | create-project |
| Contract Watch selection: status ∈ {undrafted, 2nd-round, two-way, unsigned}, ≥40 event min, ranked by status-cohort pctl; first-rounder never eligible | unit | `tests/unit/test_sl_desk_contract_watch.py` — boundary fixtures at 39/40/41 min and each status | create-project |
| Echo/read template rendering: each template family renders correct numbers, and suppresses when threshold not met | unit | `tests/unit/test_sl_desk_templates.py` — golden strings for one instance per family; suppression cases | create-project |
| Desk service returns full page payload per state (one service call; no per-player queries) | integration | `tests/integration/test_sl_desk_home.py::test_payload_shape_per_state` — seed schedule+logs, request `/` with each mocked state, assert sections present/absent per state contract | create-project |
| Tracker aggregates exact vs seeded game logs; toggles filter populations correctly; 0-GP placeholder | integration | `...::test_tracker_aggregates_and_toggles` | create-project |
| Ledger performers ordering + status tags; undrafted player ranks by metric not status | integration | `...::test_ledger_all_statuses` | create-project |
| Contract-watch historical kicker ("N of last M signed") matches join over past SL + player status history | integration | `...::test_contract_kicker_query` | create-project |
| Freshness stamp equals actual last ingest tick; stale tick (>90 min in-window) triggers lag note, never a fabricated time | integration | `...::test_freshness_honesty` — seed ingest timestamps directly | create-project |
| Unresolved players render as text, never broken links; resolved players deep-link correctly | integration | `...::test_deep_links_and_unresolved` | create-project |
| Off-window `/` renders collapsed strip only; pre-feature home content unchanged | integration | `...::test_off_window_collapse` — assert strip present + full Desk sections absent | create-project |
| `/` query budget in every state | integration (perf) | extend `tests/integration/perf/budgets.py` + `make perf` parameterized over seeded states | create-project |
| New Desk queries index-backed | integration (explain) | `make explain ROUTE=/` on prod-like branch; capture output in PR | create-project (pre-merge gate) |
| P2 Roster Wire relevance rule (only if built) | unit + integration | `tests/unit/test_sl_desk_roster_wire.py` (rule) + `...::test_roster_wire_window` (renders only Jul 5–9 window) | create-project (P2) |

## Required Post-Build QA

| Requirement | Verification Path | Evidence |
|---|---|---|
| Each state renders correctly on the live dev app (morning / live / ledger / off-window, via seeded schedule or state override) | e2e / browser (Playwright MCP, no login) | `tests/visual/screenshots/sl-desk-{morning,live,ledger,offwindow}.png` |
| Desk Wire ticker animates during live state, absent in morning state; scouting language only (no market vocabulary) | e2e / browser + text audit | `sl-desk-ticker.png` + grep of rendered page |
| Key-matchup tally, live board reads, and echo numbers match DB spot-checks | e2e / browser + SQL | annotated screenshot + query output pasted in ticket |
| Tracker toggles + column sort work in-page; rows deep-link to SL player pages | e2e / browser | `sl-desk-tracker.png` + click-through log |
| JS-disabled cold load renders all content server-side | e2e / browser | JS-off screenshot |
| Mobile (390px): no horizontal page scroll; tables scroll in-container | e2e / browser | `sl-desk-mobile.png` |
| Percentile/grade chips legible against style guide (contrast, heat direction consistent with existing SL heat-shading) | visual | `make visual` diff review |
| Live browser paint-check that page JS actually executed (ticker/toggles) — guard against the inert-script failure mode | e2e / browser | console log + interaction screenshot |

## Ticket Injection Notes

- **State resolver is its own P0 ticket** with the unit table-test as its acceptance criteria — every other UI ticket depends on it; timezone cases are non-negotiable.
- **Methodology TBDs block their tickets, not the layout:** storyline weights and the slot-cohort window rule must be fixed in the spec/ticket body before the corresponding compute tickets start; layout/template tickets can proceed against fixture payloads.
- **One service, one payload:** ticket the Desk data layer as a single service call per state (`summer_league_desk_service`) with an explicit no-per-row-query acceptance criterion, so the home-page N+1 problem doesn't compound.
- The **template-family golden tests** double as the editorial guardrail: any new sentence template requires a golden test, keeping "no editorial" enforceable in CI.
- Post-build QA must include the **live paint-check** (see `project_sl_heat_shading` lesson: ES-module script shipped inert past all non-browser tests).
- Baselines (cohort distributions, yr-2 jump, contract-conversion history) are natural candidates for **offline precompute**; if a table/materialization is added, remember `tests/integration/conftest.py` schema imports + Alembic migration in the same ticket.
