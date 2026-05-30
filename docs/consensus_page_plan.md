# Dedicated Consensus Page — Feature Plan

## Overview

DraftGuru already surfaces consensus data in three public places — the **homepage hero + supporting panels**, the **player-detail consensus block**, and the **`/sources` analytics pages** (Tier 6 of the consensus project; see master spec `docs/consensus_mock_plan.md` / issue #207, GitHub Project #2). What's missing is a **dedicated, deep, exploratory page** for the consensus board itself — the analog to the existing `/podcasts`, `/news`, and `/film-room` destinations.

This page is the home for the **full board** (every ranked player, not the homepage's lottery slice) plus the **expansive, data-nerd-oriented analytics** that were intentionally deferred from the space-constrained homepage panels (see deferred item **D5** in `consensus-mock-deferred-items`): the agreement scatter, the full source-deviation table, the source-breakdown matrix, contrarian percentile-among-peers, and player rank trajectories.

**Relationship to existing work:** this is net-new UI built almost entirely on the **already-shipped read layer** (`app/services/consensus_read_service.py`). It introduces no schema or migration changes. It cross-references the consensus master spec (#207) but ships as its own GitHub Project for a clean, self-contained orchestration run.

**Audience & goal:** fans and data-literate users who want to go beyond "who's #1" into *how* the consensus is built, *who* disagrees, and *how* it's moving. Engagement + shareability + source-credit (link-outs drive referral traffic).

## Route & navigation

- New UI route in `app/routes/ui.py`: **`GET /consensus`** (`response_class=HTMLResponse`), rendering a new `app/templates/consensus.html` extending `base.html`.
- Add a **top-nav link** ("Consensus" or "Big Board") in the shared nav, alongside NEWS / PODCASTS / FILM ROOM / STATS.
- The **homepage consensus hero** gains a "View full board →" link into this page (the hero stays the lottery-slice teaser; this page is the full experience).
- Per-page assets: `app/static/css/consensus.css` and `app/static/js/consensus.js` (loaded via `{% block extra_css %}` / `{% block extra_js %}`), following the repo's no-build, BEM, kebab-case conventions. (During orchestration these start as per-section scoped files for parallelism, then a consolidation ticket merges them back to the single per-page `consensus.css`/`.js`.)
- **Board kind is internal, not a user control.** The heading ("Consensus Big Board" / "Consensus Mock Draft") is chosen by the draft calendar via `get_consensus_board_kind()` exactly as the homepage does — there is **no toggle**. When the calendar phase is mock and no mock data exists, the board shows the existing empty state.

## Cross-cutting conventions

- **Visual target:** every UI section is built to match its section of `mockups/draftguru_consensus_page.html` and verified against it (see the QA checklist / test plan).
- **Link out to creators everywhere a source is named.** Any source/creator mention — scatter source picker/label, deviation-table rows, percentile pin, matrix column headers, spotlight — links to the producer's published board (external `work_url`, new tab, `rel="noopener"`), falling back to the internal `/sources/{slug}` page when there's no external article. This extends the aggregator-credits-sources principle shipped in PR #267.
- **Seed readiness for visual verification:** dev data is sparse (few sources, possibly one snapshot), which would make trajectories flat and the scatter/matrix/percentile thin. Every visual ticket (and the QA gate) must first seed a known demo state via `scripts/seed_synthetic_consensus_history.py` (multi-snapshot history + source divergence) so screenshots verify against meaningful data.

## Sections

### 1. Full consensus board (centerpiece)

The complete ranked board for the current snapshot — all players, not the lottery slice.

- **Data:** `get_consensus_board(db, draft_year=…)` already returns the full ordered board as `list[ConsensusRow]` (rank, Δ, recent_ranks, player/photo, school/logo, pos, ht, wt, age, avg, median, high, low, std_dev, num_sources). No new service needed for big board.
- **Columns:** rank · Δ · trend sparkline (reuse `app/utils/sparkline.py`) · player (photo) · school (logo) · pos · ht · wt · age · avg · **range bar** (high→low with consensus marker — reuse the controversy tug-of-war visual language and the consensus-marker scale fix from PR #267) · #sources · status.
- **Interactions (vanilla JS, client-side over the already-loaded rows):**
  - **Position filter** (PG/SG/SF/PF/C and hybrids) and **free-text player/school search**.
  - **Sort** by column (rank default; allow avg, high, low, #sources, age).
  - Full board is long → no lottery truncation here (that's the homepage's job).
- **Empty state:** no snapshot → friendly message, page still renders.

### 2. Source analytics (the D5 payoff — expansive viz lives here)

The destination for the visuals too space-hungry for the homepage banner.

- **2a. Agreement scatter** (per source): a square SVG plotting consensus rank (x) vs the source's rank (y) for every player on that source's board, with a 45° "perfect agreement" diagonal; off-diagonal points are the source's bold calls. A **source picker** (dropdown/tabs) switches the active source, and the active source name **links out** to its board. **Each dot is hoverable** — a tooltip names the player and their rank vs consensus (signals interactivity; the real page wires a styled tooltip). **Data:** `get_source_detail(db, source_slug=…, draft_year=…)` already returns `overlay_rows` (source_rank, consensus_rank, delta per player) — the scatter consumes those points directly. New work is the SVG component + picker + hover wiring, not new data.
- **2b. Source deviation table:** the full "Source Analytics" card from `mockups/draftguru_consensus_board.html` — every source (name links out) ranked by avg deviation / contrarian score, with their biggest outlier pick. **Data:** `get_source_leaderboard` / `get_source_analytics` (existing).
- **2c. Contrarian percentile-among-peers:** a 0–100 scale plotting every source by contrarian score, each labeled with its percentile rank in the field (name links out). Now genuinely informative here (all sources shown) vs. the homepage banner (deferred there at N=4). **Data:** the same `contrarian_score` set already fetched by `get_source_leaderboard`.
- **2d. Source breakdown matrix:** top-N players (rows) × each source (columns, headers link out), cell = that source's rank for the player, with **outlier cells highlighted** (mockup's "Source Breakdown"). **New service helper:** `get_source_breakdown_matrix(db, *, draft_year, top_n)` returning `{players: [...], sources: [...], cells: {(player_id, source_id): rank}}`, built from the per-source overlays (the same board entries `get_source_detail` reads). Outlier flag = cell deviates from consensus beyond a threshold.

### 3. Player rank trajectories

A "stock ticker" view of how players are moving across snapshots.

- **Data:** consensus history already exists — `RankHistoryPoint` per player via `get_player_consensus_detail`, and `recent_ranks` on `ConsensusRow`. For a page-level multi-player chart, add **`get_rank_trajectories(db, *, draft_year, top_n)`** returning each top-N player's ordered `(computed_at, consensus_rank)` series in one batched query (mirrors `_recent_ranks_map`).
- **Render:** a multi-line SVG chart (reuse / extend `app/utils/sparkline.py` patterns) of the top-N players' consensus rank over recent snapshots; risers/fallers color-coded. Optionally the deviation bee-swarm (deferred D5) can live here as a secondary viz.
- **Empty/flat state:** single snapshot → flat lines / "trajectories appear once multiple snapshots exist."

### 4. Richer supporting panels

The homepage panels, unbounded.

- **Full Biggest Movers** (not just top 3/3), **full Most Controversial**, and the **award-based Source Spotlight** (source names link out) — all reuse the engines built in PR #267 (`get_biggest_movers`, `get_most_controversial`, `get_source_spotlight`), just with larger limits and the extra real estate. Mostly template reuse.

## New service work (summary)

Everything else reuses the shipped read layer. Net-new service helpers:

1. `get_source_breakdown_matrix(db, *, draft_year, top_n)` — top-N × sources rank matrix with outlier flags (§3d).
2. `get_rank_trajectories(db, *, draft_year, top_n)` — batched multi-player consensus-rank-over-time series (§4).

No schema/migration changes. No new write paths.

## Suggested work breakdown (orchestrator tickets)

Decomposed so a fresh model can run them with maximum parallelism. To keep the UI section tickets **parallel** (separate worktrees can't share files without merge conflicts), the scaffold establishes a **per-section structure** — each section owns its own `partials/consensus/<section>.html` + scoped `consensus-<section>.css`/`.js`, and the route wires the full data context once — so section tickets never collide on shared files. A later consolidation ticket merges the scoped assets back to the repo's single per-page `consensus.css`/`.js` convention.

- **T-services. New page service helpers** — `get_source_breakdown_matrix` + `get_rank_trajectories` (+ unit/integration tests). *(Root — no deps.)*
- **T-scaffold. Page scaffold + route + data wiring** — `/consensus` route (calendar-kind heading, full context, no-snapshot empty state), `consensus.html` with include slots + placeholders, nav link, homepage hero "View full board" link, per-section asset shells. *(Depends on T-services so the route can wire matrix/trajectories context.)*
- **T-board. Full board table** (§1) — rich columns + range bar (with the PR #267 marker-scale guard) + client-side filter/search/sort. *(Depends on scaffold.)*
- **T-scatter. Agreement scatter + source picker** (§2a) — hoverable dots + source link-out. *(Depends on scaffold.)*
- **T-deviation. Source deviation table + contrarian percentile** (§2b, §2c) — source names link out. *(Depends on scaffold.)*
- **T-matrix. Source breakdown matrix** (§2d) — consumes `get_source_breakdown_matrix`. *(Depends on scaffold + services.)*
- **T-trajectories. Player rank trajectories** (§3) — consumes `get_rank_trajectories`. *(Depends on scaffold + services.)*
- **T-panels. Richer supporting panels** (§4) — full movers/controversial/award-spotlight. *(Depends on scaffold.)*
- **T-consolidate. Consolidate page assets to per-page convention** — merge scoped `consensus-<section>.css`/`.js` into single `consensus.css`/`.js`, update `consensus.html` blocks, delete scoped files; verify render parity + visual no-regression. *(Depends on all section tickets.)*
- **T-qa. Cross-feature QA + visual gate** (§all, opus) — full suite, parity vs `/api/consensus`, anonymous Playwright walkthrough, desktop+mobile visual capture, empty/flat-state checks. *(Depends on consolidate + all.)*

The section tickets (board, scatter, deviation, matrix, trajectories, panels) are parallel-shippable once the scaffold lands; consolidate collapses the assets; the opus QA gate is the final gate.

## Reuse inventory (already shipped — do not rebuild)

- `consensus_read_service.get_consensus_board / get_player_consensus_detail / get_source_analytics / get_source_leaderboard / get_source_detail / get_snapshots / get_biggest_movers / get_most_controversial / get_source_spotlight / get_board_freshness`.
- Models in `app/models/consensus.py` (`ConsensusRow`, `PlayerConsensusDetail`, `SourceRankEntry`, `RankHistoryPoint`, `SourceAnalyticsRow`, `SnapshotSummary`).
- `app/utils/sparkline.py` (SVG path builder), the controversy tug-of-war + source-dot CSS, the reaches/fades bar, and the award/accent-var patterns from PR #267 (`app/static/css/home.css`).
- `app/config.py:get_consensus_board_kind()` + `CONSENSUS_DRAFT_YEAR` (in `ui.py`).
- `mockups/draftguru_consensus_board.html` — the layout/styling reference for the full board + sidebar + source-analytics card.

## Out of scope / deferred

- **Mock-draft data**: populating mock consensus depends on #228 (mock extraction) — out of scope here. The page renders the calendar-determined kind; when that's mock and there's no data, it shows the empty state.
- **A methodology / "how it's built" explainer** — deliberately cut (too wordy for this audience).
- **Rigorous "Ahead of the Curve"** (per-source board-history version) and other items beyond the four sections stay deferred (D5).
- No changes to the homepage panels (PR #267) beyond adding the "View full board" link.

## Verification

Per `docs/plans/ai-orchestrator-ticket-spec.md`: backend tickets → unit + integration; UI tickets → integration + e2e (Playwright) + visual (`make visual`). New service helpers get unit/integration coverage; page sections get integration (route context + rendered markup) + visual verification at desktop and mobile widths, including empty/flat-data states. Conda env `draftguru`; dev server per the repo override.
