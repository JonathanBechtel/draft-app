# Summer League Cron Desk-Starvation Refactor Test Plan

**Sources:**
- Tech spec: `docs/plans/summer-league-cron-desk-starvation-spec.md` (GitHub issue [#622](https://github.com/JonathanBechtel/draft-app/issues/622))

**Sibling artifact:** QA checklist at `summer-league-cron-desk-starvation-qa-checklist.md`

## Purpose

The product risk here is availability, not correctness of displayed data: a slow/greedy full-ingestion cron can starve the live Desk for over an hour, and a bad reconciliation step can leave prod cron machines silently running stale code. Tests must prove (a) the Desk is never blocked beyond a bounded wait, (b) routine ingestion is fast because it skips unchanged games, (c) normalization is safe to interrupt and resume, and (d) deploy-time image drift is caught rather than silently ignored. This repo's existing `tests/unit/` (no DB) / `tests/integration/` (real Postgres via `TEST_DATABASE_URL` + `PYTEST_ALLOW_DB=1`) split is used as-is; concurrency/lock tests belong in `tests/integration/` since they require real Postgres advisory locks — SQLite/mocked sessions cannot exercise `pg_advisory_xact_lock` semantics.

## Required Build-Time Tests

| Requirement | Test Type | Suggested Test | Ticket Mapping |
|---|---|---|---|
| Desk lock acquisition is bounded, not blocking-forever | unit | `tests/unit/services/summer_league/test_write_lock.py` — new bounded-wait helper returns/raises after configured timeout when lock unavailable (fake clock, no real Postgres) | create-project (Slice 1: Containment) |
| Desk tick respects bounded wait against a real contended lock | integration | `tests/integration/test_sl_desk_tick.py` — hold `pg_advisory_xact_lock` in one session, run `run_desk_tick`/equivalent in a second concurrent session, assert it returns within the bound | create-project (Slice 1: Containment) |
| Full ingestion releases the writer lock at short, predictable checkpoints (not one venue-wide `db.begin()`) | integration | `tests/integration/test_summer_league_ingest_runner.py` (new, mirroring existing `tests/unit/cli/test_summer_league_ingest_runner.py` but against real DB) — assert lock is released/reacquired between backbone / shot-batch / PBP-batch phases, not held continuously across all three | create-project (Slice 1: Containment) |
| Concurrent Desk + full-ingestion run with no deadlock and correct priority handoff | integration | `tests/integration/test_sl_desk_ingestion_concurrency.py` (new) — two real async sessions racing for the lock across several iterations; assert no deadlock exception and Desk-side completion time stays bounded | create-project (Slice 1: Containment) |
| Ingestion is resumable after interruption without replaying completed batches | integration | Extend `tests/integration/test_summer_league_pipeline_state.py` (or new dirty-game-tracking equivalent) — simulate a mid-run failure after N of M batches commit, re-run, assert only remaining batches execute and row counts match a clean full run | create-project (Slice 1: Containment / Slice 2: Performance) |
| Only changed/dirty games are normalized on routine runs | integration | `tests/integration/test_summer_league_normalization.py` (extend) — run normalization twice against identical fixture files; second run's `games_processed`/upsert counts are ~0 | create-project (Slice 2: Performance) |
| Explicit full-reconciliation/repair mode still replays everything on demand | integration | `tests/integration/test_summer_league_ingest_runner.py` (extend) — invoke repair mode explicitly; assert full game set reprocessed regardless of dirty-tracking state | create-project (Slice 2: Performance) |
| Bulk shot/PBP normalization uses preloaded identities and chunked `INSERT ... ON CONFLICT`, no per-event `flush()` | unit + integration | Unit: assert no `await db.flush()` call sits inside the per-shot/per-PBP-row loop (static/behavioral check via a flush-counting fake session in `tests/unit/services/summer_league/test_normalization_bulk.py`). Integration: `tests/integration/test_summer_league_normalization.py` (extend) — normalize a 70+ game fixture and assert identical output vs. the pre-refactor row-by-row path (golden-row diff) | create-project (Slice 2: Performance) |
| A realistic 70+ game fixture shows material duration reduction from the 87.7-minute baseline | integration (perf) | New perf-style test/benchmark script under `tests/integration/perf/` (mirrors `tests/integration/perf/budgets.py` pattern) using a synthetic 70-game, 10k-shot-event fixture; asserts wall-clock (or DB-round-trip count) drops materially vs. a recorded pre-refactor baseline | create-project (Slice 2: Performance) |
| No provider/network/embedding call happens while a DB transaction or advisory lock is held | unit | `tests/unit/cli/test_summer_league_ingest_runner.py` (extend) — call-order-recording fake `NBAStatsClient`/Gemini client asserts no call occurs between lock-acquire and lock-release timestamps | create-project (Slice 1: Containment) |
| Desk cron image reconciles automatically (or fails loudly) after a stop-wait timeout | integration/CI | New script test for the reconciliation logic extracted from `.github/workflows/fly-deploy-prod.yml`'s "Update Summer League Desk cron machine" step (e.g. `scripts/reconcile_cron_image.py` + `tests/unit/scripts/test_reconcile_cron_image.py`) — simulate a still-running machine at wait-timeout; assert a retry/alert path fires instead of a bare warning | create-project (Slice 3: Deployment reliability) |
| Post-deploy verification detects image-digest drift per cron machine | unit | `tests/unit/scripts/test_verify_cron_image_digests.py` (new) — given mocked `flyctl machine list` JSON with one machine on a stale digest, assert the check reports/fails for that machine by name | create-project (Slice 3: Deployment reliability) |
| Writer-lock wait, batch duration, dirty-game backlog, and freshness are structured, greppable telemetry | unit | `tests/unit/test_summer_league_pipeline_telemetry.py` (extend) — assert new telemetry fields (`writer_lock_wait_ms`, per-batch `games_processed`, dirty-game backlog count) are present in emitted log records | create-project (Slice 4: Production validation) |
| A normal Desk tick stays under two minutes with healthy providers | integration (perf) | `tests/integration/perf/budgets.py` (extend) — add/confirm a Desk-tick duration budget alongside existing query-count budgets | create-project (Slice 4: Production validation) |
| Existing deferred-reconciliation behavior is unchanged | integration | Re-run `tests/integration/test_summer_league_pipeline_state.py` and `tests/unit/cli/test_summer_league_ingest_runner.py` unmodified (or with additive-only changes) as a regression gate | create-project (all slices, regression gate) |

## Required Post-Build QA

| Requirement | Verification Path | Evidence |
|---|---|---|
| Desk stays responsive during a real (staged) full-ingestion run | manual / staging | Trigger a staging full-ingestion run against a large fixture while polling `/summer-league`; screenshot + timestamp showing fresh data mid-run |
| Prod deploy leaves all Summer League cron machines on the current image | manual (post-deploy) | `flyctl machine list --app draft-app-prod --json` diffed against the deployed app image digest for `summer-league-ingestion-cron`, `summer-league-desk-cron`, `summer-league-roster-cron` |
| Production duration budgets hold under real venue traffic | manual (first live event after ship) | Compare production `pipeline_telemetry` log durations for the next live Summer League session against this plan's targets (venue phase materially under 87.7 min; Desk tick under 2 min) |

## Ticket Injection Notes

- Ticket: Bound and prioritize Desk lock acquisition (Slice 1)
  - Required tests: unit test for the bounded-wait helper's timeout behavior (fake clock); integration test proving a real contended `pg_advisory_xact_lock` still lets the Desk tick return within the bound; integration concurrency test for no-deadlock + priority handoff.
  - Required telemetry: `writer_lock_wait_ms` emitted for both Desk and ingestion jobs.

- Ticket: Split full-ingestion transactions into per-game/per-phase batches (Slice 1)
  - Required tests: integration test asserting the lock is released/reacquired between backbone, shot-batch, and PBP-batch phases (not held across all three in one `db.begin()`); resumability test simulating a crash after N of M batches.
  - Required DB assertions: no duplicate rows in `summer_league_shot_events`/`summer_league_play_by_play_events` after a resumed run vs. a clean run.

- Ticket: Track and process only changed/dirty games (Slice 2)
  - Required tests: two-run idempotency test (second run touches ~0 games); explicit full-reconciliation mode still reprocesses everything on demand.
  - Required DB assertions: dirty-game tracking state clears for games actually processed and stays set for games still pending.

- Ticket: Bulk event normalization — preload identities, chunked upserts, no per-event flush (Slice 2)
  - Required tests: flush-counting fake session proving zero flushes inside the per-event loop; golden-row diff proving output parity with the pre-refactor row-by-row path on a 70+ game fixture; perf test showing material wall-clock reduction from the 87.7-minute baseline.
  - Required invariant: no `NBAStatsClient`/Gemini/provider call between lock-acquire and lock-release.

- Ticket: Reliable cron image reconciliation + drift verification (Slice 3)
  - Required tests: reconciliation-retry unit test for the stop-wait-timeout case; post-deploy digest-verification script test covering the "one machine stale" case.
  - Required behavior: a stale/drifted machine produces a failed check or actionable alert, never a silent `::warning::`-only skip.

- Ticket: Operational safeguards — lock-wait, batch, backlog, and freshness telemetry (Slice 4)
  - Required tests: telemetry emission tests for each new field; Desk-tick duration budget added to `tests/integration/perf/budgets.py`.
  - Required regression gate: existing `tests/integration/test_summer_league_pipeline_state.py` and `tests/unit/cli/test_summer_league_ingest_runner.py` pass unmodified/additively.
