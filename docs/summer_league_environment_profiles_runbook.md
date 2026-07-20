# Competition Context (Summer League Environment Profiles) — Operator Runbook

Covers the versioned `summer_league_environment_profiles` projection (#606
schema, #617 aggregation, #618 pipeline wiring). See
`docs/plans/competition-context-explorer-implementation-contract.md` for the
frozen data/operational contract this runbook implements against — in
particular §8 (rebuild/backfill/publication locking).

## 1. How it stays fresh

- **Historical backfill** runs once before the Explorer's public modules are
  enabled (§8: "the historical backfill runs before public modules are
  enabled"). See [§3 Manual rebuild](#3-manual-rebuild--backfill).
- **Incremental refresh** is wired into the hourly Summer League ingest cron
  (`app/cli/summer_league_ingest_runner.py`). Inside that runner's locked
  `metrics_and_snapshots` phase — after normalized facts (`_run_venue`'s
  backbone/shot/PBP normalization) and advanced metrics
  (`rebuild_sl_metrics`) are materialized, and while the transaction still
  holds the shared Summer League writer lock —
  `app.services.summer_league.environment_refresh.resolve_environment_refresh_scope`
  decides whether this cycle's configured year (`SL_INGEST_YEAR`) needs a
  refresh (any venue found games, or a prior deferral is pending), and
  `refresh_environment_profiles_for_year` reuses that same session/lock to
  call the #617 aggregation contract
  (`rebuild_environment_profiles(db, year=...)`), which rebuilds that year's
  all-competitions season scope plus every competition scope in it.
- A quiet cycle (no games, nothing pending) touches nothing — the previous
  published profiles stay current.

## 2. Locking

Every publish (rebuild, or the rollback in §4) acquires the shared,
transaction-scoped Summer League writer lock
(`app.services.summer_league.write_lock.acquire_summer_league_writer_lock`,
`pg_advisory_xact_lock`) **before its first source read**, and holds that
same transaction through calculation, validation, version insertion, and the
atomic `is_current` switch. The lock is re-entrant within one transaction, so
the pipeline-invoked incremental refresh (which runs inside the ingest
runner's already-locked transaction) does not wait on itself. A standalone
rebuild or rollback opens its own transaction and acquires the lock fresh —
it will block behind (or block) a concurrent pipeline run or another
standalone operation, never interleave with it.

## 3. Manual rebuild / backfill

`scripts/rebuild_summer_league_environment.py` — three rebuild modes, plus
rollback (§4):

```bash
# Full historical rebuild (every competition + every season scope).
scripts/with-db-env.sh conda run -n draftguru \
  python scripts/rebuild_summer_league_environment.py

# One year's season scope + every competition scope in that year.
scripts/with-db-env.sh conda run -n draftguru \
  python scripts/rebuild_summer_league_environment.py --year 2025

# Exactly one competition scope (no season scope).
scripts/with-db-env.sh conda run -n draftguru \
  python scripts/rebuild_summer_league_environment.py --competition-id 42
```

Idempotent: re-running always publishes a fresh version and atomically flips
`is_current`; a failed candidate leaves the prior current profile in place
and is reported as a per-scope failure (exit output lists `FAILED
<scope_key>: <reason>`), never silently swallowed. Never mutates raw Summer
League facts (`SummerLeagueGame`/`*GameLog`/`*ShotEvent`/etc.).

Run this before flipping on any public Competition Context module for a
season that hasn't been backfilled yet, and after any correction to raw
Summer League facts (identity resolution fixes, a corrected box score, etc.)
that should retroactively change a profile beyond what the next incremental
cycle would naturally pick up.

## 4. Rollback to a prior current version

Every published version is retained (never deleted) — only `is_current`
moves. If a freshly published version is judged wrong after the fact (a
validated-but-substantively-bad rebuild, e.g. from bad upstream source data
that still passed validation), restore an older version without
recomputing anything:

```bash
scripts/with-db-env.sh conda run -n draftguru \
  python scripts/rebuild_summer_league_environment.py \
  --rollback-scope-key season:2025 --rollback-to-version 3
```

`--rollback-scope-key` accepts the stable scope key exactly as stored
(`season:<year>` or `competition:<competition_id>`) — never a display label
or a row ID (contract §2: "Projection row IDs are never public URL
identifiers"). This calls
`app.services.summer_league.environment_refresh.rollback_environment_profile`,
which acquires the writer lock, demotes the current row, and promotes the
target version to current in one transaction (same atomic-switch guarantee
as a rebuild). A rollback to the version that is already current is a
reported no-op and changes nothing.

This is distinct from a rebuild-time validation failure: that case (#617)
already keeps the prior current profile automatically and needs no manual
intervention. Rollback is only for reversing an already-*published* version.

## 5. Inspect / verify

Check what's currently published for a scope:

```sql
SELECT scope_key, version, is_current, calculated_at, source_watermark,
       final_games, box_complete_games, shot_covered_games
FROM summer_league_environment_profiles
WHERE scope_key = 'season:2025'
ORDER BY version DESC;
```

Per-metric coverage/reason for the current row:

```sql
SELECT c.metric_key, c.coverage, c.covered_games, c.eligible_games, c.reason
FROM summer_league_environment_metric_coverage c
JOIN summer_league_environment_profiles p ON p.id = c.profile_id
WHERE p.scope_key = 'season:2025' AND p.is_current;
```

Last incremental-refresh outcome (durable pipeline state, updated by every
pipeline-invoked refresh — not by a manual `scripts/rebuild_summer_league_environment.py`
run, which only prints its own result):

```sql
SELECT job, last_outcome, last_succeeded_at, last_failure_at, last_failure_reason
FROM summer_league_pipeline_states
WHERE job = 'environment_refresh';
```

## 6. Stale-profile behavior

Public reads compare a profile's `calculated_at` against
`settings.summer_league_environment_stale_after_hours` (default **48**,
override via `SUMMER_LEAGUE_ENVIRONMENT_STALE_AFTER_HOURS`) via
`app.services.summer_league.environment_refresh.is_environment_profile_stale`
— the single shared helper every consumer should call rather than
re-deriving the threshold. Past the threshold, the profile is still the last
good, fully readable version; it only gains a stale badge in the UI. It is
**never** silently replaced by a request-time recompute (contract §8).

The 48-hour default tolerates a quiet weekend/off-cycle gap during an active
event without falsely flagging normal quiet periods, while still catching a
genuinely stuck incremental refresh (e.g. the writer lock contended for
several cycles in a row, or a repeatedly failing rebuild). A **dormant
historical season's** profile (computed once during backfill, with no future
games to ever trigger a refresh) will always read as stale under this simple
age check once 48 hours pass — that is intentional honesty ("last computed
on `<date>`"), not a bug: it tells a reader exactly how current the number
they're looking at is, without claiming freshness it doesn't have.

## 7. Recovery from a stuck or failed run

1. **Check the durable state** (§5's `summer_league_pipeline_states` query,
   job `environment_refresh`) and the pipeline logs for
   `environment_refresh_run` / `environment_refresh_scope_failed` structured
   log lines (`app/services/summer_league/environment_refresh.py`), which
   report requested/built/skipped/failed scope counts, metric coverage,
   input watermark, and duration for every attempt.
2. **A single bad cycle** (e.g. the writer lock was contended, or a
   transient error) typically self-heals: the next hourly ingest cycle that
   sees `any_games=True` for that year retries the refresh automatically
   (the underlying rebuild is idempotent — see §3). No action needed unless
   the failure repeats across multiple cycles.
3. **A repeatedly failing refresh** (same `last_failure_reason` across
   several cycles, or a season stuck with no venue producing new games to
   naturally retrigger a refresh): run the manual rebuild for that year
   (§3). This opens a fresh, independently locked transaction and is safe to
   run at any time, including while the ingest cron is between cycles.
4. **A published-but-wrong version**: use rollback (§4) rather than trying to
   "fix forward" with another rebuild if the underlying raw-fact problem
   hasn't been corrected yet — a rebuild against still-bad source data would
   just republish the same problem under a new version number.
5. **Verify the fix** with the §5 SQL: confirm `is_current` points at the
   expected version and `calculated_at` is recent (or, for a rollback,
   confirm `previous_current_version` in the command's printed output
   matches what you expected to move away from).

Raw Summer League facts are never touched by any of the above — recovery
here only ever changes which projected version is `is_current`.
