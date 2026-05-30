# Dedicated Consensus Page QA Checklist

**Sources:**
- Tech spec: `docs/consensus_page_plan.md`

**Sibling artifact:** test plan at `consensus-page-test-plan.md`

This checklist defines product-level behaviors QA should verify before considering the dedicated consensus page complete. The page is a **public, read-only** surface built on the existing consensus read layer — there is no auth, no writes, and no new persistence. Verification is anonymous (no login) per `docs/plans/ai-orchestrator-ticket-spec.md`.

## Core User Behaviors

- A visitor can reach the full consensus board from the top nav.
  - Verify: load any page → click the "Consensus" (Big Board) nav link.
  - Expected: navigates to `/consensus`; the full board renders.
  - Evidence: browser at `/consensus`, board table visible.

- A visitor can get to the full board from the homepage hero.
  - Verify: on `/`, click "View full board →" under the consensus hero.
  - Expected: lands on `/consensus`.
  - Evidence: navigation + URL.

- The board shows **every** ranked player, not just the lottery slice.
  - Verify: compare the row count on `/consensus` to the homepage hero's lottery slice.
  - Expected: `/consensus` shows the complete ordered board (all players in the snapshot); homepage still shows only the lottery teaser.
  - Evidence: row counts; last row rank > lottery cutoff.

- Each board row shows the full column set with a working range bar.
  - Verify: inspect a row.
  - Expected: rank · Δ · trend sparkline · player (photo when available) · school (logo when available) · pos · ht · wt · age · avg · range bar (high→low with the consensus marker **on** the track) · #sources · status.
  - Expected (negative): a player's consensus marker never renders outside the range track (regression guard from PR #267).
  - Evidence: screenshot; marker position within track for all rows.

- A visitor can filter, search, and sort the board client-side.
  - Verify: apply a position filter; type a player/school in search; click a sortable column header (avg, high, low, #sources, age).
  - Expected: rows filter/sort without a page reload; clearing restores the full board.
  - Evidence: row set changes on interaction.

- The board heading reflects the calendar-determined kind — there is no user toggle.
  - Verify: load `/consensus`; inspect the heading.
  - Expected: heading is "Consensus Big Board" or "Consensus Mock Draft" per `get_consensus_board_kind()` (no toggle control present). When the calendar kind is mock and no mock data exists, the board shows the empty state.
  - Evidence: heading text matches the calendar phase; no toggle in the DOM.

- A visitor can see how an individual source agrees with consensus (agreement scatter), and probe each pick.
  - Verify: in source analytics, pick a source; hover a dot.
  - Expected: a scatter plots that source's rank (y) vs consensus rank (x) with a 45° agreement diagonal; off-diagonal points reflect that source's bold calls; switching sources re-renders; hovering a dot shows a tooltip with the player + their rank vs consensus.
  - Evidence: scatter for ≥2 different sources; tooltip on hover.

- A visitor can compare all sources' deviation and contrarianism.
  - Verify: view the source deviation table + contrarian percentile scale.
  - Expected: every contributing source appears, ranked by avg deviation / contrarian score, with its biggest outlier; the percentile scale plots each source and labels the active one's percentile.
  - Evidence: table row per source; percentile marker.

- A visitor can read the source-breakdown matrix and spot outliers.
  - Verify: view the top-N × sources matrix.
  - Expected: each cell is a source's rank for that player; cells that deviate beyond the outlier threshold are visually highlighted.
  - Evidence: matrix rendered; ≥1 highlighted outlier cell when divergence exists.

- Every source/creator mention links out to their work.
  - Verify: across the scatter (active source), deviation table, percentile pin, matrix column headers, and the spotlight, inspect each source name.
  - Expected: each links to the producer's published board (external, `target="_blank"` + `rel="noopener"`), falling back to `/sources/{slug}` when no external article exists.
  - Evidence: anchor attributes on every source mention.

- A visitor can see how players are trending across snapshots.
  - Verify: view the rank-trajectories chart.
  - Expected: top-N players' consensus rank over recent snapshots as lines; risers/fallers color-coded.
  - Expected (negative): with a single snapshot, lines are flat / a "trajectories appear once multiple snapshots exist" state shows — no error.
  - Evidence: multi-line chart, or flat-state.

- A visitor can consume the richer supporting panels.
  - Verify: view Biggest Movers, Most Controversial, and Source Spotlight on the page.
  - Expected: full-length movers (more than the homepage 3/3), full controversial list, and the award-based spotlight; source link-outs open the producer's published board in a new tab (fallback to `/sources/{slug}`).
  - Evidence: panel content; external link `target=_blank` + rel `noopener`.

## Persistence And Data Integrity

- The page reflects the current consensus snapshot (read-only; no writes).
  - Verify: load `/consensus`; compare ranks/avg/high/low to `/api/consensus` for the same draft year.
  - Expected: board values match the API for the latest snapshot; the page issues no write/mutation.
  - Evidence: parity between page and API payload; no DB mutation in logs.

- Source-breakdown and trajectory data are consistent with the board.
  - Verify: cross-check a matrix cell and a trajectory endpoint against `get_source_detail` / consensus history for the same player+source.
  - Expected: ranks agree.
  - Evidence: spot-check values match.

## Scope, Auth, And Safety

- The page is fully public — no login required.
  - Verify: load `/consensus` anonymously (no session).
  - Expected: 200, full content.
  - Evidence: anonymous request succeeds.

- Outbound source links are safe and credit the producer.
  - Verify: inspect a "read their board" link.
  - Expected: external links use `target="_blank"` + `rel="noopener noreferrer"`; sources without an external article fall back to the internal `/sources/{slug}` page.
  - Evidence: rendered anchor attributes.

## Operational Behavior

- The page renders fast and degrades gracefully with sparse data.
  - Verify: load with current dev data (few sources, possibly 1 snapshot).
  - Expected: no 500s; every section either shows data or a clean empty/flat state (no-snapshot, single-snapshot, mock-draft, players without photos/logos).
  - Evidence: each section's empty/flat path screenshotted.

- The page is responsive.
  - Verify: load at desktop and mobile widths.
  - Expected: board table scrolls/reflows; analytics + panels stack cleanly; no overflow/overlap.
  - Evidence: desktop + mobile screenshots.

## Final Browser QA

After seeding the demo state (`scripts/seed_synthetic_consensus_history.py`), anonymous Playwright pass over `/consensus`, with each section checked against `mockups/draftguru_consensus_page.html`:
  - Board renders with all rows; markers on-track; filter/search/sort work; heading matches the calendar kind (no toggle).
  - Each analytics sub-section renders (scatter for ≥2 sources with hover tooltips, deviation table, percentile, matrix with an outlier highlight).
  - Trajectories render (or flat-state on single snapshot).
  - Panels render; every source mention links out (external attributes correct).
  - Desktop + mobile screenshots saved under `tests/visual/screenshots/`.

## Completion Bar

The feature is product-complete when QA can demonstrate:
1. `/consensus` is reachable from nav + homepage hero and renders the full board (all players) with correct columns and on-track range markers; the heading reflects the calendar kind with no toggle.
2. Filter, search, and sort all work client-side; the calendar-mock-with-no-data case shows a clean empty state.
3. The source-analytics suite — agreement scatter (source picker + hover tooltips), deviation table, contrarian percentile, and outlier-highlighted breakdown matrix — all render and agree with `/api/consensus` / source data.
4. Player rank trajectories render (or show the flat/single-snapshot state).
5. The richer movers/controversial/spotlight panels render.
6. Every source/creator mention links out to their published board (external, with internal `/sources/{slug}` fallback).
7. All sections degrade gracefully (no-snapshot, single-snapshot, mock-empty, missing photos), match the mockup, and the page is responsive at desktop + mobile.
