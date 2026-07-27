# Summer League Cron Desk-Starvation Refactor QA Checklist

**Sources:**
- Tech spec: `docs/plans/summer-league-cron-desk-starvation-spec.md` (GitHub issue [#622](https://github.com/JonathanBechtel/draft-app/issues/622))

**Sibling artifact:** test plan at `summer-league-cron-desk-starvation-test-plan.md`

This checklist defines product- and operator-level behaviors QA should verify before considering the cron-desk-starvation refactor complete. There is no new end-user UI surface; "the user" here is primarily the **Summer League Desk page visitor** (who needs fresh scores) and the **on-call operator** (who needs the pipeline to be observable and recoverable). Baseline reference numbers are the July 19, 2026 production incident cited in the spec: 87.7-minute venue phase, 79-minute Desk lock wait, 10,155 shot events across 70 games in one critical section.

## Core User Behaviors

- A Desk page visitor should see live scores update promptly even while full ingestion is running.
  - Verify: with a full-ingestion run artificially held open (long-running venue phase, e.g. a test double that sleeps mid-normalization), trigger a Desk tick (`app/cli/sl_desk_tick.py`) concurrently.
  - Expected: the Desk tick does not block for the full ingestion duration; it either acquires the lock within its bounded wait and completes, or cleanly yields/retries within that bound rather than hanging.
  - Evidence: Desk tick log shows `writer_lock_wait` step duration ≤ the documented bound; Desk-visible snapshot (`materialize_desk_render_snapshots` output / `/summer-league` page) reflects current scores after the tick completes.

- A Desk tick should never wait indefinitely for the writer lock.
  - Verify: inspect `app/cli/sl_desk_tick.py`'s lock-acquisition call (currently `acquire_summer_league_writer_lock`, a blocking `pg_advisory_xact_lock`) after the fix.
  - Expected: acquisition uses a bounded-wait primitive (e.g. `pg_try_advisory_xact_lock` retried within a timeout, or `SET LOCAL lock_timeout`) with an explicit maximum wait, not an unbounded blocking wait.
  - Evidence: code path plus a concurrency integration test asserting the Desk tick returns/logs within the bound when the lock is held by a long-running competitor.

- A normal Desk tick completes quickly when providers are healthy.
  - Verify: run `app/cli/sl_desk_tick.py` end-to-end against fixture data with mocked NBA Stats responses and no lock contention.
  - Expected: total tick duration stays under two minutes (spec's stated budget); comparable to the observed ~38s uncontended baseline.
  - Evidence: `pipeline_telemetry` run-level log line (`summer_league_pipeline_run ... duration_ms=...`) under the two-minute threshold.

- Routine (non-repair) ingestion only touches games that actually changed.
  - Verify: run the ingest runner twice back-to-back against the same raw fixture set with no new/changed game files between runs.
  - Expected: the second run's normalization phases process zero (or near-zero) games; it does not re-normalize all 70+ games each hour.
  - Evidence: normalization report / telemetry step shows `games_processed` ≈ 0 on the no-op second run, versus the full count on the first run.

- An explicit full-reconciliation/repair mode still exists and works.
  - Verify: invoke the repair/full-rebuild path (env var or CLI flag) against a competition with previously-normalized games.
  - Expected: it re-processes the full game set on demand, distinct from the routine changed-only path.
  - Evidence: telemetry/log line identifying the run as a full-reconciliation pass; row counts match a full replay.

## Persistence And Data Integrity

- Full ingestion is safely resumable after an interruption.
  - Verify: kill (or simulate a crash mid-way through) an ingest run after 1-2 of several per-game/per-phase batches have committed, then re-run.
  - Expected: the re-run does not replay already-committed batches (no duplicate work, no double-counted upserts) and completes the remaining/incomplete batches.
  - Evidence: `SummerLeaguePipelineState`/dirty-game-tracking rows before and after; row counts in `summer_league_shot_events` / `summer_league_play_by_play_events` unchanged for already-completed games, populated for the resumed ones.

- Shot and PBP normalization produce identical output whether run in one pass or resumed across batches.
  - Verify: run a fixture set straight through vs. interrupted-and-resumed; diff the resulting `summer_league_shot_events` / `summer_league_play_by_play_events` rows.
  - Expected: byte-for-byte identical row sets (idempotent upsert keyed on `(nba_stats_game_id, nba_stats_game_event_id)` / `(nba_stats_game_id, event_num)`), no duplicates from a partially-committed-then-repeated batch.
  - Evidence: row-count and content diff in the integration test.

- No external provider or embedding request runs while a DB transaction or the advisory writer lock is held.
  - Verify: audit every `async with db.begin():` / lock-held block touched by this refactor (`app/cli/summer_league_ingest_runner.py`, `app/services/summer_league/normalization.py`) for network calls inside the block.
  - Expected: NBA Stats HTTP fetches and any Gemini/embedding calls happen strictly outside lock/transaction scope, matching the existing pattern already used for the raw-fetch steps.
  - Evidence: code review + a test asserting no `NBAStatsClient`/provider call happens between lock-acquire and lock-release (e.g. via a call-order-recording fake).

- Existing deferred-reconciliation behavior remains correct.
  - Verify: re-run `tests/integration/test_summer_league_pipeline_state.py` and the deferred-reconciliation scenarios in `tests/unit/cli/test_summer_league_ingest_runner.py`.
  - Expected: `defer_full_reconciliation` / `full_reconciliation_is_pending` / `complete_pipeline` semantics from the prior cron-coordination work (#576-era) are unchanged: a lock-contended run still marks `pending_reconciliation=True` and a later successful run still clears it.
  - Evidence: passing test suite; no behavior regression in `pipeline_state.py`.

## Scope, Auth, And Safety

- Concurrent Desk and full-ingestion runs never deadlock or corrupt shared identity rows.
  - Verify: run a concurrency integration test that starts a full-ingestion batch and a Desk tick against overlapping game/player identities at the same time (real Postgres, not mocked locks).
  - Expected: no deadlock, no lost update on `SummerLeagueSourcePlayer`/`PlayerAffiliation` identity rows; whichever job yields does so cleanly without leaving a half-written batch.
  - Evidence: test passes deterministically across repeated runs (flag flakiness); DB state after both complete matches the expected merged result.

- Priority handoff is correct: the Desk is never starved by ingestion re-acquiring the lock immediately after releasing it.
  - Verify: simulate ingestion configured to reacquire the lock in a tight loop across small batches while a Desk tick is concurrently waiting.
  - Expected: the Desk tick gets priority within its bounded wait rather than being repeatedly out-raced by ingestion's next batch.
  - Evidence: concurrency test asserting Desk-tick completion time stays within its bound even under adversarial ingestion batch cadence.

- A missing/invalid config (bad `SL_INGEST_YEAR`, etc.) still fails loudly rather than silently ingesting nothing — unchanged by this refactor.
  - Verify: re-run existing `_resolve_year`/`_resolve_league_ids` unit tests.
  - Expected: no regression — invalid config still returns exit code 1 with a logged error.
  - Evidence: existing unit tests in `tests/unit/cli/test_summer_league_ingest_runner.py` continue to pass unmodified (or with equivalent coverage).

## Operational Behavior

- Writer-lock wait duration is captured as structured telemetry, per job.
  - Verify: inspect log output from both a contended and uncontended Desk tick and ingestion batch.
  - Expected: a distinct, greppable field for lock-wait duration (not folded into a generic step duration), for both the Desk and ingestion jobs.
  - Evidence: log line (e.g. `writer_lock_wait_ms=...`) present and numerically distinguishable from total step duration.

- Batch/phase duration and per-batch games/events processed are observable.
  - Verify: run a multi-batch ingestion pass.
  - Expected: each batch emits its own duration and games/events-processed counts (not just one venue-wide aggregate).
  - Evidence: one telemetry log line per batch, e.g. `summer_league_pipeline_step ... step=venue:15:shot_batch:<n> games=... events=... duration_ms=...`.

- Dirty-game backlog is observable.
  - Verify: after a run that defers/skips some games, query or log the outstanding dirty-game count.
  - Expected: an operator can tell how many games are still pending normalization without reading raw file timestamps.
  - Evidence: a queryable table/column or log line reporting backlog size.

- Last successful Desk tick and snapshot freshness are observable.
  - Verify: check `SummerLeaguePipelineState` (or equivalent) after a Desk tick.
  - Expected: `last_succeeded_at` / `last_snapshots_materialized_at` (already present in `pipeline_state.py`) are populated for the Desk job specifically, not only `FULL_INGESTION`.
  - Evidence: DB row inspection after a live Desk tick run.

- Production cron image drift is detected and surfaced as an actionable failure, not a silent warning.
  - Verify: simulate the Desk cron machine failing to stop within its wait window during a prod deploy (`.github/workflows/fly-deploy-prod.yml`, "Update Summer League Desk cron machine" step).
  - Expected: the workflow either retries/reconciles automatically on a later run, or fails the deploy check / posts an actionable alert — not just `::warning::` with no follow-up.
  - Evidence: workflow run log / a post-deploy verification job comparing each cron machine's image digest to the deployed app image digest and failing if they differ beyond an expected grace window.
  - Note: `machine start` re-arm behavior (already fixed for the schedule-drop incident) must be preserved by any change here.

- Post-deploy verification confirms every Summer League cron machine (ingestion, Desk, roster) is on the current image digest.
  - Verify: run the new digest-verification step/script after a prod deploy.
  - Expected: pass/fail per cron machine name, not just "no machine found."
  - Evidence: script output or workflow annotation listing each machine's current vs. desired digest.

## Final Browser QA

- The Summer League Desk / homepage live score module reflects a recent tick even during a concurrent full-ingestion run.
  - Verify: with a full-ingestion run artificially extended (test/staging only), load `/summer-league` (or the homepage live-score module) and check the displayed "last updated" / score freshness.
  - Expected: freshness reflects a Desk tick that completed within its two-minute budget, not one blocked for the ingestion run's duration.
  - Evidence: screenshot with visible timestamp/score state; cross-checked against the Desk tick's telemetry timestamp for that run. Manual QA on staging/dev is acceptable here — this is a timing/ops property, not a new UI, so it does not need `make visual` static screenshots.

## Completion Bar

The feature is product-complete when QA can demonstrate:
1. A held full-ingestion lock cannot block a Desk tick beyond one bounded batch, verified by a real (non-mocked) concurrent Postgres integration test.
2. Routine ingestion measurably skips unchanged games, and a realistic 70+ game fixture shows a material reduction from the 87.7-minute baseline venue-phase duration.
3. Shot/PBP normalization is resumable and idempotent across interrupted runs with no per-event `flush()` remaining in the hot path.
4. No provider/network call occurs while a DB transaction or the advisory writer lock is held, anywhere in the refactored paths.
5. Lock-wait, batch, backlog, and freshness telemetry are all observable in logs/DB after a run.
6. A prod deploy either reconciles cron image drift automatically or fails the check loudly; there is no more silent warning-only skip.
7. All existing deferred-reconciliation and config-validation behavior is unchanged.
8. `make precommit`, full `mypy app --ignore-missing-imports`, relevant unit/integration tests, and `make coverage.diff` all pass under the `draftguru` Conda environment.
