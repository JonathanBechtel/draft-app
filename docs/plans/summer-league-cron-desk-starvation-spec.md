# Refactor Summer League Cron Pipeline to Prevent Live Desk Starvation

> Source: [GitHub issue #622](https://github.com/JonathanBechtel/draft-app/issues/622). This is the issue body verbatim, promoted to a spec doc so it can drive `/create-qa-checklist` and `/create-project`.

## Problem

The Summer League full-ingestion cron can hold the shared writer lock for more than an hour, starving the hourly Summer League Desk cron and delaying live homepage score updates.

The recent cron-coordination work improved transaction boundaries around Desk provider calls and added deferred reconciliation, but it does not solve the core contention pattern:

- Full ingestion can acquire the writer lock before the Desk.
- The Desk waits indefinitely on that lock.
- Full ingestion performs backbone rebuilding, shot normalization, and play-by-play normalization inside one transaction/lock scope.
- Event normalization performs large amounts of row-by-row identity resolution, flushing, and upserting.
- Full ingestion revisits an entire venue even when only a small number of games changed.

This makes the live pipeline dependent on the completion time of historical/maintenance processing.

## Production evidence

On July 19, 2026:

- Full ingestion started at approximately 03:09 UTC.
- The venue phase reported `duration_ms=5261671.3` — approximately **87.7 minutes**.
- The run normalized **10,155 shot events** across 70 games before continuing through PBP normalization.
- PostgreSQL showed the ingestion connection idle in transaction while holding the Summer League advisory writer lock.
- The Desk cron was blocked on that advisory lock for roughly 79 minutes.
- Once contention cleared, a fresh Desk tick completed in approximately **38 seconds**.

This demonstrates that the apparent hour-long Desk runtime is primarily lock waiting, not live-tick computation.

## Current hot spots

- `app/cli/summer_league_ingest_runner.py`: `backfill_summer_league_backbone`, `normalize_shot_events`, and `normalize_pbp_events` share one `db.begin()` block and one advisory-lock lifetime.
- `app/services/summer_league/normalization.py`: shot normalization resolves/upserts a source player and flushes for each event; PBP normalization similarly resolves actors and upserts per event.
- `app/cli/sl_desk_tick.py`: the higher-priority Desk uses a blocking advisory-lock acquisition with no bounded wait.
- `.github/workflows/fly-deploy-prod.yml`: a failed wait for the Desk machine to stop leaves it on an older image, with only a warning and no later reconciliation.

## Desired architecture

Treat Summer League processing as three cooperative lanes:

1. **Live Desk lane**
   - Fetch scoreboard/current-game data.
   - Normalize only affected games.
   - Rebuild the public Desk snapshot.
   - Receive priority over maintenance processing.
   - Never wait indefinitely for full ingestion.

2. **Incremental normalization lane**
   - Process only changed/dirty games.
   - Commit after each game or small bounded batch.
   - Release the advisory lock between batches.
   - Yield when the Desk needs the writer.
   - Resume safely after interruption.

3. **Deferred derivative lane**
   - Rebuild broader metrics/snapshots after normalization.
   - Defer cleanly when live work has priority.
   - Recover deferred work on a later run without replaying unrelated ingestion.

## Proposed work

### 1. Bound and prioritize Desk lock acquisition

- Add a bounded lock-wait policy for the Desk with structured timing.
- Ensure full ingestion releases the writer lock at predictable short checkpoints.
- Add cooperative priority/handoff semantics so ingestion does not immediately reclaim the lock while a Desk tick is waiting.
- Preserve single-writer safety for shared identities and projections.

### 2. Split full-ingestion transactions

Replace the venue-wide critical section with independently committed phases/batches:

- Backbone normalization.
- Shot normalization by game or small game batch.
- PBP normalization by game or small game batch.
- Metrics/snapshot rebuild.

Each phase must be idempotent and resumable. A failure or machine restart should replay only the incomplete batch.

### 3. Process changed games only

- Track which raw game files changed during the fetch phase.
- Feed only changed/dirty game IDs into normalization.
- Keep an explicit full-reconciliation/repair mode for intentional historical rebuilds.
- Do not perform a full 73-game event replay during routine live-season ingestion.

### 4. Bulk event normalization

- Preload source-player, actor, team, and game mappings once per batch.
- Bulk-upsert missing source identities.
- Use chunked PostgreSQL `INSERT ... ON CONFLICT` operations for shots and PBP.
- Remove per-event `flush()` calls and avoid per-event lookup queries.
- Keep all Gemini/provider/network work outside database transactions and advisory-lock scopes.

### 5. Make cron image reconciliation reliable

- Retry/reconcile the Desk cron image after an in-flight tick stops.
- Verify all production cron image digests after deployment.
- Surface image drift as a failed check or actionable alert rather than a warning-only skip.
- Continue to re-arm the Fly scheduled machine after an image update.

### 6. Add operational safeguards

Emit and monitor:

- Writer-lock wait duration per job.
- Transaction/batch duration.
- Games and events processed per batch.
- Dirty-game backlog.
- Last successful Desk tick and snapshot freshness.
- Current versus desired image digest for each cron.

## Acceptance criteria

- [ ] A running full-ingestion cron cannot block a Desk tick for longer than one bounded normalization batch.
- [ ] Desk writer-lock wait has an explicit maximum and structured telemetry.
- [ ] Routine ingestion normalizes only changed/dirty games.
- [ ] Shot and PBP normalization use batch-preloaded identities and chunked upserts; no per-event flush remains.
- [ ] No external provider or embedding request runs while a DB transaction or advisory writer lock is held.
- [ ] Full ingestion is safely resumable after interruption without replaying completed batches.
- [ ] Concurrent Desk/full-ingestion integration tests demonstrate no deadlock and correct priority handoff.
- [ ] A realistic 70+ game fixture demonstrates a material reduction from the observed 87.7-minute venue phase.
- [ ] A normal Desk tick remains under two minutes when provider APIs are healthy.
- [ ] Production deployment verifies that the Desk and ingestion cron machines use the current app image.
- [ ] Existing deferred-reconciliation behavior remains correct.
- [ ] `make precommit`, full `mypy app --ignore-missing-imports`, relevant unit/integration tests, and patch coverage checks pass under the `draftguru` Conda environment.

## Suggested delivery slices

1. **Containment:** bounded Desk wait, per-game/per-phase transactions, and concurrency tests.
2. **Performance:** changed-game filtering plus bulk shot/PBP normalization.
3. **Deployment reliability:** cron image reconciliation and drift verification.
4. **Production validation:** duration budgets, lock-wait telemetry, and monitored rollout.

## Non-goals

- Changing public Summer League Desk presentation or scoring semantics.
- Replacing the canonical player-journey/backbone model.
- Folding the separate homepage live-score projection correctness fix into this refactor.
