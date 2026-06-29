# 2026 Summer League Roster Foundation — QA Checklist

**Sources:**
- Pitch: `docs/plans/summer-league-2026-roster-foundation-pitch.md`
- Feature plan (Workstream 0 + A in scope): `docs/plans/summer-league-2026-full-roster.md`
- Schema sketch (ticket-ready DDL): `docs/plans/summer-league-2026-workstream0-schema.md`
- Backbone design: `docs/plans/global-player-journey-graph.md`
- Repo orchestration guide: `docs/plans/ai-orchestrator-ticket-spec.md`

**Sibling artifact:** test plan at `summer-league-2026-roster-foundation-test-plan.md`

This checklist defines product-level behaviors QA should verify before the slice is
considered complete. The slice is a **backend data-foundation pipeline**: there are no
public routes, templates, share cards, or admin UI in scope (the public roster preview,
A4, is deferred). The acceptance bar is: a clean reversible migration, a robust
daily-pollable roster scraper, an idempotent refreshable loader, deterministic player
resolution, and — above all — an **append-only roster history that is reconstructable and
never overwritten**, born on the canonical journey-graph grain so no future restructuring
migration of these rows is required.

Logical tickets referenced below (T-IDs) are mapped to GitHub issue numbers by
`/create-project`.

## Schema & Migration (T1)

- The migration creates both new tables and the additive column cleanly.
  - Verify: `alembic upgrade head` on a disposable DB.
  - Expected: `player_affiliations` and `summer_league_participation` tables exist with all
    columns, FKs, indexes, and uniqueness constraints from the schema doc; the two enum
    types (`affiliation_type_enum`, `affiliation_status_enum`) are created exactly once;
    `summer_league_player_game_logs.participation_id` exists as a nullable FK with its index.
  - Evidence: schema introspection + the integration schema test.

- The migration downgrades cleanly with no residue.
  - Verify: `alembic downgrade base` after upgrade on a disposable DB.
  - Expected: both new tables, the added column, and both enum types are dropped; the
    pre-existing `summer_league_player_game_logs` table is otherwise unchanged (never
    dropped/recreated).
  - Evidence: downgrade run output + introspection showing the column and enums gone.

- The additive column does not disturb existing game-log data.
  - Verify: with pre-existing player-game-log rows present, run the upgrade.
  - Expected: existing rows gain `participation_id = NULL`; no row is rewritten; the
    column is nullable.
  - Evidence: row counts and null-state assertion before/after.

## Roster Scraper (T2)

- The scraper enumerates every team for the pilot venue from the landing page.
  - Verify: run against a captured NBA.com venue landing-page fixture.
  - Expected: every team link + `TeamID` for the venue is discovered (e.g. 30 for Vegas,
    7 for California Classic); no team silently dropped.
  - Evidence: discovered-team list vs. expected count.

- The scraper parses the embedded roster JSON, not scraped HTML text.
  - Verify: parse a captured per-team page fixture.
  - Expected: reads `__NEXT_DATA__.props.pageProps.roster`; each player yields `PLAYER_ID`
    (PERSON_ID), `NUM`, `POSITION`, `HEIGHT`, `WEIGHT`, `BIRTH_DATE`, `SCHOOL`,
    `HOW_ACQUIRED`, `LeagueID`, `TeamID`.
  - Evidence: parsed records for a known team match the fixture.

- The currently-empty roster state is handled gracefully (critical near-term).
  - Verify: parse a page whose `roster` array is `[]` (the live state today).
  - Expected: no crash; the team is reported with zero players; the run completes and
    records "0 rostered" rather than erroring.
  - Evidence: empty-roster fixture test passing; run report shows zero counts.

- The scraper is idempotent and writes one raw snapshot per run.
  - Verify: run twice against the same fixtures.
  - Expected: a deterministic raw JSON snapshot per run under the SL raw layout; safe to
    re-run; no partial-write corruption on the snapshot.
  - Evidence: snapshot file diff stable; run report.

- Transient per-team fetch failures don't abort the whole run.
  - Verify: simulate one team page failing.
  - Expected: that team is recorded as an error; the remaining teams still load; the run
    reports the failure without erasing successful work.
  - Evidence: run report with per-team error captured.

## Idempotent Refresh Loader (T3)

- Each rostered player becomes a source player keyed on PERSON_ID.
  - Verify: load a fixture roster into a disposable DB.
  - Expected: one `summer_league_source_players` row per unique `nba_stats_person_id`;
    re-running does not duplicate it.
  - Evidence: row counts after first and second load.

- A first load creates one ANNOUNCED affiliation assertion + one participation row per
  rostered player.
  - Verify: load a fixture roster.
  - Expected: one `player_affiliations` row (`status=ANNOUNCED`, `affiliation_type=
    SUMMER_LEAGUE_ROSTER`, `recorded_at` set, `superseded_at`/`retracted_at` null) and one
    `summer_league_participation` row (correct competition/team_entry/source_player/stint)
    per player; competition + team_entry rows exist for the venue.
  - Evidence: assertion + participation rows per player.

- Re-running with an unchanged source is fully idempotent.
  - Verify: load the same fixture twice.
  - Expected: **no new** affiliation assertions, no new participation rows, no overwrites;
    the diff report shows all players "unchanged".
  - Evidence: identical row counts after both runs; diff report.

- The loader emits a roster-diff report.
  - Verify: load roster v1, then a modified roster v2.
  - Expected: report lists added / unchanged / cut counts per team that match the actual
    changes.
  - Evidence: diff report contents.

## Append-Only History Invariant (T3 — highest value)

- A late-added player produces a new ANNOUNCED assertion, no mutation of others.
  - Verify: load v1, then v2 with one extra player.
  - Expected: exactly one new `player_affiliations` row (ANNOUNCED) for the new player;
    all prior rows unchanged.
  - Evidence: row-level before/after.

- A dropped player produces a superseding CUT assertion; the prior is never deleted.
  - Verify: load v1, then v2 with one player removed.
  - Expected: a new `player_affiliations` row (`status=CUT`, `supersedes_id` = the prior
    assertion's id); the prior row still exists with `superseded_at` set (not deleted, not
    overwritten); the participation row persists with `roster_status=CUT`.
  - Evidence: both rows present; supersession chain intact.

- Roster history is reconstructable at any point in time.
  - Verify: after v1→v2→v3 with adds and cuts, query the assertion stream.
  - Expected: "who was announced on the team as of <date>" and "who was cut and when" are
    both answerable from `player_affiliations` alone (recorded_at + supersession), with no
    information lost.
  - Evidence: point-in-time reconstruction query returning the correct membership.

## Player Resolution & Backfill (T4)

- PERSON_ID drives deterministic resolution.
  - Verify: load a roster where a player already has a `player_external_ids(system=
    'nba_stats')` mapping.
  - Expected: the source player resolves with `EXTERNAL_ID` status to the existing
    canonical player — no fuzzy match needed.
  - Evidence: resolution status + canonical link.

- Unmatched players become stubs only with `--create-stubs`.
  - Verify: load a roster with an unknown player, with and without the flag.
  - Expected: with the flag, a `players_master` stub (`is_stub=True`) is created and linked
    (`STUB`); without it, the source player stays `UNRESOLVED` (or queued for review) and
    is not silently guessed.
  - Evidence: stub row / unresolved state per flag.

- Resolution backfills canonical ids onto participation and affiliation.
  - Verify: resolve after loading.
  - Expected: `summer_league_participation.player_id` and `player_affiliations.player_id`
    are populated to the canonical id for resolved players.
  - Evidence: backfilled ids on both tables.

## Foundation / No-Rewrite Acceptance

- 2026 data sits on the canonical grain with no rewrite required.
  - Verify: inspect the loaded pilot-venue data.
  - Expected: roster history lives in append-only `player_affiliations`; the stable
    `summer_league_participation` bridge is the grain future game logs will FK; a
    maintainer confirms no restructuring/backfill of these rows is needed when Workstreams
    B/C/D and the deferred backbone pieces land (they are additive).
  - Evidence: a short maintainer sign-off note referencing the schema doc's "no future
    rewrite" rationale.

## Out of Scope (do not QA in this slice)

As-played box-score ingestion (B), image/bio/college enrichment (C), ops/scheduling/prod
replication (D), the public roster preview UI (A4), the other two venues, and Workstream 0
Tier 1 (`assertion_evidence` provenance + `player_identity_action` audit). No visual or
browser testing is required — there is no UI in this slice.
