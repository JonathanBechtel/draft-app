# Dedicated Consensus Page Test Plan

**Sources:**
- Tech spec: `docs/consensus_page_plan.md`

**Sibling artifact:** QA checklist at `consensus-page-qa-checklist.md`

## Purpose

The page is read-only UI over the shipped consensus read layer, so the product risk is **wrong/missing rendering and broken interactions**, not data corruption. The two net-new service helpers (`get_source_breakdown_matrix`, `get_rank_trajectories`) carry the only real logic risk — they need unit/integration coverage. Everything else is route-context + rendered-markup integration tests plus visual/e2e verification, including the empty/flat-data states that dominate dev today (few sources, possibly one snapshot, no mock data).

Test tiers follow `docs/plans/ai-orchestrator-ticket-spec.md`: `tests/unit/` (no DB), `tests/integration/` (DB + FastAPI via HTTPX; needs `TEST_DATABASE_URL` + `PYTEST_ALLOW_DB=1`; schema modules imported in `tests/integration/conftest.py`), `tests/visual/` (Playwright, output `tests/visual/screenshots/`). Public page → anonymous verification, no login.

## Required Build-Time Tests

| Requirement | Test Type | Suggested Test | Ticket Mapping |
|---|---|---|---|
| `/consensus` returns 200 and renders the board template for the current snapshot | integration | `tests/integration/test_consensus_page.py::test_page_renders` | create-project (scaffold) |
| `/consensus` reachable: nav link + homepage hero "View full board" link present | integration | `tests/integration/test_consensus_page.py::test_nav_and_hero_links` | create-project (scaffold) |
| No-snapshot → page renders with empty state, no 500 | integration | `tests/integration/test_consensus_page.py::test_empty_state_no_snapshot` | create-project (scaffold) |
| Board shows all players (not lottery-sliced) with full column set + range bar markup | integration | `tests/integration/test_consensus_page.py::test_board_full_and_columns` | create-project (board) |
| Range marker percentage stays within 0–100 even when consensus_rank is outside a player's high/low (regression from PR #267) | unit | `tests/unit/test_consensus_marker_scale.py` (pure scale calc) | create-project (board) |
| Heading reflects the calendar kind (no toggle); calendar-mock-with-no-data shows empty state | integration | `tests/integration/test_consensus_page.py::test_kind_heading_and_empty` | create-project (board) |
| Filter/search/sort operate client-side over rendered rows | e2e | Playwright: filter/search/sort change the visible row set without reload | create-project (board) |
| Agreement scatter renders points for a selected source; picker switches sources; dots have hover tooltips | integration + e2e | integration asserts scatter markup/data + per-dot tooltip for ≥1 source; Playwright switches picker + hovers a dot | create-project (scatter) |
| Every source/creator mention links out (external `target=_blank`/`rel=noopener`, internal `/sources/{slug}` fallback) across scatter, deviation table, percentile, matrix headers, panels | integration | `tests/integration/test_consensus_page.py::test_source_linkouts` | create-project (scatter, deviation, matrix, panels) |
| Source deviation table lists all sources w/ contrarian score + outlier; percentile scale plots each | integration | `tests/integration/test_consensus_page.py::test_source_deviation_and_percentile` | create-project (deviation/percentile) |
| `get_source_breakdown_matrix(db, *, draft_year, top_n)` returns correct top-N × sources cells + outlier flags | unit + integration | unit for cell/outlier logic with fixture overlays; integration against a seeded multi-source snapshot | create-project (matrix) |
| Breakdown matrix renders with highlighted outlier cells | integration | `tests/integration/test_consensus_page.py::test_breakdown_matrix_markup` | create-project (matrix) |
| `get_rank_trajectories(db, *, draft_year, top_n)` returns each top-N player's ordered (computed_at, rank) series in one batch | unit + integration | unit for ordering/series shape; integration across ≥2 seeded snapshots | create-project (trajectories) |
| Trajectories chart renders multi-line; single-snapshot → flat/empty state | integration | `tests/integration/test_consensus_page.py::test_trajectories_states` | create-project (trajectories) |
| Richer panels render (full movers/controversial/award spotlight) with correct external link-out attributes | integration | `tests/integration/test_consensus_page.py::test_panels_and_linkouts` | create-project (panels) |
| Board/matrix/trajectory values agree with `/api/consensus` for the same snapshot | integration | parity assertion against the read API | create-project (QA gate) |

## Required Post-Build QA

| Requirement | Verification Path | Evidence |
|---|---|---|
| Full anonymous walkthrough of `/consensus` (after seeding demo state) | e2e / Playwright (anonymous) | board, filter, search, sort functional; each section matches `mockups/draftguru_consensus_page.html` |
| Every analytics sub-section renders with seeded data | browser | scatter (≥2 sources, hover tooltip), deviation table, percentile, matrix w/ outlier highlight |
| Every source mention links out | browser | external anchor attributes across all sections |
| Empty/flat-data states | browser | no-snapshot, single-snapshot trajectories, calendar-mock-empty, missing-photo rows |
| Responsive layout | visual (`make visual` + ad-hoc `browser_take_screenshot`) | desktop + mobile PNGs under `tests/visual/screenshots/` |
| Range markers never overflow the track | visual | screenshot of board rows |

## Ticket Injection Notes

All UI tickets build toward their section of `mockups/draftguru_consensus_page.html` and seed the demo state (`scripts/seed_synthetic_consensus_history.py`) before visual/e2e verification.

- **Ticket: New page service helpers**
  - Required tests: `get_source_breakdown_matrix` unit (cells + outlier flags) + integration (seeded multi-source snapshot); `get_rank_trajectories` unit (series ordering/shape) + integration (≥2 snapshots).
  - Files flag: `app/services/consensus_read_service.py`, `tests/unit`, `tests/integration` (no new schema import expected — reads existing tables).

- **Ticket: Page scaffold + route + nav**
  - Required tests: `/consensus` 200 + template render; calendar-kind heading (no toggle); nav link + homepage hero link present; no-snapshot empty state (no 500); route wires the full context (incl. the two new helpers).
  - Files flag: new `app/routes/ui.py` handler (thin; delegates to `consensus_read_service`), `app/templates/consensus.html` + `partials/consensus/*` placeholders, per-section scoped `consensus-*.css/js` shells, `partials/navbar.html`, `home.html` hero link.

- **Ticket: Full board table**
  - Required tests: all-players (not lottery) + full columns + range-bar markup; **unit test for the marker-scale guard** (consensus_rank outside high/low stays in 0–100); calendar-kind heading + empty state; e2e filter/search/sort. No toggle.
  - Note: reuse `get_consensus_board` (already full board); reuse sparkline util + PR #267 range/marker CSS + scale fix.

- **Ticket: Agreement scatter + source picker**
  - Required tests: scatter data/markup for a source from `get_source_detail` overlay; per-dot hover tooltip; active-source link-out; e2e picker switch + dot hover.

- **Ticket: Source deviation table + contrarian percentile**
  - Required tests: all sources listed (names link out) with contrarian score + biggest outlier; percentile scale marks each source. Reuse `get_source_leaderboard` / `get_source_analytics`.

- **Ticket: Source breakdown matrix**
  - Required tests: matrix markup with highlighted outliers; column headers link out. Consumes `get_source_breakdown_matrix` (from the service helpers ticket).

- **Ticket: Player rank trajectories**
  - Required tests: chart multi-line render + single-snapshot flat state. Consumes `get_rank_trajectories`; reuse `app/utils/sparkline.py` patterns for the multi-line SVG.

- **Ticket: Richer supporting panels**
  - Required tests: full movers/controversial/award-spotlight render; every source mention links out (external `target=_blank` + `rel=noopener`; internal fallback to `/sources/{slug}`).
  - Note: reuse `get_biggest_movers` / `get_most_controversial` / `get_source_spotlight` with larger limits.

- **Ticket: Consolidate page assets to per-page convention**
  - Required tests: render parity (page renders identically post-merge) + visual no-regression after merging scoped `consensus-*.css/js` into single `consensus.css`/`.js` and updating the template blocks.

- **Ticket: Cross-feature QA + visual gate (opus)**
  - Required: seed demo state; parity vs `/api/consensus`; full anonymous Playwright walkthrough vs the mockup; source-linkout sweep; desktop + mobile visual capture; all empty/flat states; marker-overflow visual check. Use the anonymous recipe from `docs/plans/ai-orchestrator-ticket-spec.md`.
