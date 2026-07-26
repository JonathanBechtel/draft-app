# Summer League Desk Launch Readiness

## Status

Approved remediation plan for the post-implementation audit of `feature/summer-league-desk`.

This plan deliberately excludes the Codex review findings already being addressed on the
feature branch. Existing issues #525 (game-grain cohort baseline) and #527 (opening-morning
scoreboard bootstrap) are prerequisites and must be reused rather than duplicated.

## Problem

The Summer League Desk has a strong visual shell and a sound assertion-versus-projection
architecture, but it is not ready for a useful production deployment. The deployed system
does not yet have a complete recurring-job setup, its schedule ingest discards matchup and
score data, live game snapshots do not refresh reliably, several analytics do not match the
product contract, and the homepage reconstructs hourly content through dozens of remote
database round trips on every request.

The work below turns the feature into a trustworthy operational read model while preserving
the Global Player-Journey Graph boundary: canonical games, team entries, participation, and
stat lines remain assertions; Desk baselines, grades, storylines, commentary, and render
snapshots remain replaceable projections.

## Goals

- Produce real Morning, Live, and Ledger states from canonical schedule and game data.
- Refresh in-progress box scores without incorrectly finalizing games.
- Deploy and verify the Desk baseline and recurring tick in staging and production.
- Correct debut, live-deviation, Ledger, Tracker, lifecycle, and freshness semantics.
- Reduce a cold homepage Desk read to no more than five database queries and a sub-500 ms
  service target against a production-like Neon branch.
- Remove roster-sized and game-player-sized query growth from the hourly tick.
- Complete the observable UI requirements: running lines, sorting, slate disclosure,
  Summer League deep links, and explicit attribution.
- Give small implementation agents narrowly bounded, independently verifiable tickets.

## Non-goals

- Replacing the Event Desk framework or the T1-T4 projection model.
- Adding a parallel source of player, team, game, or stat truth.
- Reworking unrelated Summer League Explorer or player-page functionality.
- Adding new share-card formats or unrelated growth features.
- Reopening the Codex findings already fixed or actively being fixed on the feature branch.

## Architectural decisions

### Canonical live data

The scoreboard enriches `summer_league_games` and its canonical team-entry links. Live box
score refreshes replace raw endpoint snapshots only for selected active games. Normalization
must preserve provider-backed Scheduled/In-Progress/Final state and must never mark a partial
snapshot Final merely because it was normalized.

### Persisted render snapshots

The homepage must not rebuild an hourly read model for every visitor. Add a generic Event
Desk render-snapshot projection keyed by event, daily state, Tracker cohort, and Tracker stat
view. The hourly tick materializes Preview, Live, and Recap variants. A request performs a
minimal current-time/calendar resolution and loads the matching snapshot.

This is a persistent database projection, not an in-process TTL cache, so cold Fly workers
receive the same fast path. Request-time state transitions remain correct because state
selection happens at request time over already-materialized state variants.

### Performance contracts

- No more than five Desk-related queries for a cold homepage request.
- No request-time query count proportional to players or games.
- Sub-500 ms Desk service target against the production-like Neon test branch.
- Tick query growth proportional to competitions/games, not roster-player combinations.
- Every changed public-page query must be checked with `make explain ROUTE=/`.

## Work breakdown

The implementation is intentionally grouped into coherent 30–60 minute Sonnet-class slices.
This avoids making the remediation project larger than the feature through ticket and merge
overhead while keeping each contract independently verifiable.

1. **Canonical schedule ingest** — parse and persist matchup teams, scores, status text, and
   the complete active-event schedule.
2. **Targeted live box-score refresh** — add exact game-ID selection and force-refresh only
   Scheduled/In-Progress game endpoints.
3. **Status-aware normalization and tick wiring** — preserve live status and run refresh before
   normalization/projections without claiming false freshness.
4. **Desk deployment readiness** — add preflight/post-tick checks and idempotent stage/prod
   cron deployment with baseline gates.
5. **Debut and game-grain Ledger correctness** — build debut distributions from first games,
   trigger debut once, and use the game-grain baseline for Ledger percentiles.
6. **Realized Live deviation and running lines** — rank current lines against the game-grain
   cohort and render both featured subjects' live PTS/REB/AST/GmSc.
7. **Tracker correctness** — honor persisted T2 gating and exact prior-year sophomore membership.
8. **Lifecycle, ownership, and freshness correctness** — retain quiet schedules and Wind-down
   Ledger, respect ownership/kill switches, and render honest stale/missing freshness.
9. **Render snapshot persistence** — add the generic snapshot table, migration, typed codec, and
   repository operations.
10. **Snapshot materialization and fast reads** — build all state/Tracker variants at tick time
    and select the correct snapshot at request time without expensive fallback reconstruction.
11. **Homepage and tick performance enforcement** — batch grading/storyline/fact work, enforce
    the five-query homepage ceiling, measure latency, and verify query plans/indexes.
12. **UI interaction completion** — add accessible Tracker sorting, signal-aware slate collapse,
    Summer League deep links, and explicit Desk attribution.
13. **Final launch-readiness QA gate** — run after all work and prerequisites #525/#527.

## General definition of done

Every work ticket must satisfy the following in addition to its ticket-specific acceptance
criteria:

- Add or update a focused test that fails without the implementation.
- Keep routes thin and keep canonical assertions separate from replaceable projections.
- Preserve caller-controlled database transactions and deterministic ordering.
- Run `conda run -n draftguru make precommit`.
- Run `conda run -n draftguru mypy app --ignore-missing-imports`.
- Run the ticket's focused unit and/or integration tests.
- Run `conda run -n draftguru make coverage.diff` and retain at least 80% patch coverage on
  changed `app/` lines.
- For changed public-page queries, run `conda run -n draftguru make perf` and
  `conda run -n draftguru make explain ROUTE=/` against a production-like database.
- For UI work, run `conda run -n draftguru make visual`, inspect the PNG output, and complete
  the anonymous browser recipe in `docs/plans/ai-orchestrator-ticket-spec.md`.
- Do not commit credentials, provider payloads containing secrets, or unrelated worktree
  artifacts.

## Test strategy

### Unit

Use provider fixtures and pure helper tests for payload parsing, status mapping, snapshot
serialization, cohort math, live deviation, sorting metadata, and disclosure policy.

### Integration

Use the disposable Postgres integration fixtures for canonical schedule upserts, partial-live
normalization, baseline lookup, tick materialization, snapshot persistence/readback, lifecycle
selection, gating, and route query counts. At least one vertical integration test must start
with provider fixture data and finish with the rendered homepage state.

### Performance

Assert query count on a cold request. Capture wall-clock service time separately so a budget
cannot pass merely by consolidating queries into one slow scan. Verify remaining statements
with `EXPLAIN ANALYZE` against a production-like Neon branch.

### Browser and visual

Verify Morning, Live, Ledger, quiet slate, stale data, Wind-down, mobile, sorting, slate
expansion, and JavaScript-disabled rendering. Deep-link and attribution checks must follow a
real click rather than inspecting only static HTML strings.

### Operations

Validate Fly configuration syntax, idempotent machine creation/update, baseline readiness,
one successful staging tick, a fresh `event_desk_state`, and a populated render snapshot
before production enablement.

## Final QA gate

The final gate runs the full suite and repository checks, audits whether tests can actually
fail when their subject is broken, reviews the combined diff against this plan and the
original Desk behavior/QA documents, checks query plans and latency, and performs the full
browser/visual matrix. It is an Opus-class synthesis ticket and may fix cross-ticket
regressions, but must not silently waive unmet launch criteria.
