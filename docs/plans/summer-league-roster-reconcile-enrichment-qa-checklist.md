# Summer League Roster Reconcile, QA & Enrichment — QA Checklist

**Sources:**
- Tech spec: `docs/plans/summer-league-roster-reconcile-enrichment-spec.md`

**Sibling artifact:** test plan at `summer-league-roster-reconcile-enrichment-test-plan.md`

This checklist defines the observable behaviors QA should verify before considering the reconcile / QA / enrichment-targeting work complete. Backend-only feature — no browser/visual QA; evidence is DB rows, CLI output, and QA-harness output.

## Core Behaviors — Reconcile (B2)

- A roster player who never appears in any box score is flagged as *announced-not-played*.
  - Verify: a competition has an `nba_summer_league_roster` participation whose `source_player_id` has **zero** rows in `summer_league_player_game_logs` for that competition.
  - Expected: the reconcile report lists that player under `announced_not_played` with name + team.
  - Evidence: `reconcile_competition()` return value; reconcile CLI output.

- A box-score player who was never on the announced roster is flagged as *played-not-announced* (late-add).
  - Verify: a `source_player_id` has ≥1 game-log row in the competition but **no** roster-sourced participation.
  - Expected: the report lists that player under `played_not_announced` with name + team.
  - Evidence: reconcile report / CLI output.

- A player who was both announced and appeared is **not** flagged.
  - Verify: an announced participation whose `source_player_id` has ≥1 game-log row.
  - Expected: excluded from both flagged lists; counted in `announced_and_played`.
  - Evidence: report totals.

- Reconcile joins game logs on `(competition_id, source_player_id)`, not `participation_id`.
  - Verify: pre-B1 game-log rows (NULL `participation_id`) still count as "played".
  - Expected: a rostered player with only NULL-`participation_id` logs is classified as played (not a false DNP).
  - Evidence: report on a seeded row with `participation_id=NULL`.

- Reconcile is read-only.
  - Verify: run reconcile twice.
  - Expected: no rows created/updated; identical report; DB row counts unchanged.
  - Evidence: before/after counts of participation / affiliation / game-log tables.

- Reconcile CLI produces a readable per-competition report.
  - Verify: `scripts/reconcile_summer_league_rosters.py --year 2025 --league-id 13`.
  - Expected: prints totals + the two flagged lists; exit 0; works against the loaded 2025 CA Classic / SLC data.
  - Evidence: CLI stdout.

## Core Behaviors — Affiliation Heal (T3)

- A box-score-first participation gains an ANNOUNCED roster assertion when later announced.
  - Verify: create a participation via the box-score path (`nba_summer_league_box_score`, CONFIRMED); then load a roster snapshot that includes that player.
  - Expected: an `ANNOUNCED` `nba_summer_league_roster` affiliation is appended (superseding), and the prior `box_score` assertion is retained (append-only, `superseded_at` set on it, not deleted).
  - Evidence: `player_affiliations` rows for that player — both assertions present, chain linked via `supersedes_id`.

- The heal is idempotent.
  - Verify: re-load the same roster snapshot.
  - Expected: no duplicate ANNOUNCED assertion is created on the second load.
  - Evidence: affiliation count stable across re-loads.

- A normal announced player (never box-score-first) is unaffected.
  - Verify: standard announced→played flow.
  - Expected: no extra/duplicate assertions from the heal branch.
  - Evidence: affiliation history unchanged vs. pre-heal behavior.

## Core Behaviors — QA Integration (B3)

- Reconcile findings appear in the SL QA harness as accepted warnings.
  - Verify: run the QA harness sliced to a competition with known DNP/late-add players.
  - Expected: reconcile counts (`announced_not_played`, `played_not_announced`) reported as **warnings**, not blocking findings; harness still exits per its normal blocking rules.
  - Evidence: QA harness output; exit code unchanged by reconcile warnings alone.

## Core Behaviors — Fetch Count (T5)

- The fetcher's reported player count equals the persisted snapshot count.
  - Verify: run the fetcher on a fixture where a person appears under two team subpages; compare the summary `players=N` to the unique entry count written to the snapshot.
  - Expected: `players=N` == unique entries in the snapshot (no double-count).
  - Evidence: fetcher summary line vs. snapshot JSON length.

## Core Behaviors — Enrichment Cohort Targeting (T0, C2/C3/C4)

- The SL-cohort selector returns the resolved players with a participation in a scope.
  - Verify: seed participations across two competitions; call the cohort selector with a year/league filter.
  - Expected: returns exactly the resolved (`player_id IS NOT NULL`) players in that scope; excludes unresolved and out-of-scope players.
  - Evidence: selector return set.

- Bio enrichment can target the SL cohort.
  - Verify: run the bio path with the SL-cohort selection.
  - Expected: the target set is restricted to cohort players with a `bbref` external id; players without a bbref id are reported to a manual-review list, not errored.
  - Evidence: target-set size; manual-review list contents.

- College-stats enrichment can target the SL cohort.
  - Verify: run the college path with SL-cohort selection.
  - Expected: targets resolved cohort players with `school` + bbref id; non-NCAA/international players report "no source" (flagged, not failed).
  - Evidence: target-set + no-source list.

- Image generation can target the SL cohort without spending.
  - Verify: run the image `--batch` submit path against the SL cohort with the vision/generation call stubbed.
  - Expected: the batch is *built* over cohort players missing a stylized image (consuming `reference_image_url`); no live Gemini spend in the test.
  - Evidence: built batch contents; stub asserted (no real API call).

## Negative / Edge Cases

- Empty competition (no participations, no logs) → reconcile returns zero-filled report, no error.
- Unresolved-only roster (all `player_id` NULL) → cohort selector returns empty; enrichment target set empty; no crash.
- A cut player (roster affiliation, status CUT) that never played → still classified announced-not-played.
- Enrichment no-source players (internationals) are always enumerated, never silently dropped.

## Completion Bar

The feature is product-complete when QA can demonstrate:
1. Reconcile flags DNP/cut and late-add players correctly on the loaded 2025 CA Classic + SLC data, read-only.
2. A box-score-first participation gains a retained-plus-ANNOUNCED affiliation chain on later roster load, idempotently.
3. Reconcile counts surface as accepted warnings in the QA harness without changing its blocking behavior.
4. The fetcher's reported count matches the persisted snapshot.
5. Each enrichment script can target just the SL cohort, with no-source players enumerated.
6. Repo checks green (precommit, mypy, unit + integration, ≥80% patch coverage).
