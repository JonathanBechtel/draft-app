# Summer League 2027 Preflight Runbook

## Status

Runbook for a **human to execute**, not an agent — same shape and same caveat as
`docs/plans/summer-league-desk-536-deploy-runbook.md`: provisioning/starting/stopping real
Fly machines and running scripts against a real Neon database branch are real-infrastructure
mutations, deliberately kept out of automated ticket work. Every command below that touches
real infrastructure is marked **[MUTATES INFRA]**; everything else is **[READ-ONLY]** and
safe to re-run at any time.

Origin: ticket #718, filed from the post-merge review of PR #706 (`390b9b7`) recorded in
`docs/plans/summer-league-remediation-roadmap.md`'s Phase 1 status section. Master reference:
#743.

Historical trend archive (dev/staging first): `scripts/backfill_sl_daily_trend_versions.py --year YYYY --dry-run` (ticket #759; use the same `with-db-env.sh` wrapper before a real run).

## Why this exists

Phase 1's remaining risk sits in exactly two shapes: things that **cannot be verified
off-season**, and manual operator sequences that **nothing schedules**. Both are dangerous for
the same reason the roadmap calls out about incident #669: "operational acceptance deferred
past merge" was the decisive process defect there, not a code defect. This runbook makes that
deferred acceptance dated and owned instead of implicit, specifically for two things #706
introduced:

1. **The desk latency-class partition (#699 / PR #706) is dormant on production by default.**
   Production machine *creation* is double-gated — the `enable_desk_class_crons`
   `workflow_dispatch` input on `.github/workflows/fly-deploy-prod.yml` (default `false`) AND
   production's own Desk preflight (`scripts/check_sl_desk_readiness.py preflight`) — and the
   point of no return, stopping the composite `summer-league-desk-cron` machine, is deliberately
   left manual (see `deploy/fly/fly.cron.desk.fast.toml`'s "PROMOTION" comment). If nobody
   executes the promotion sequence before Vegas 2027, production keeps running the pre-#699
   composite tick — which is safe, but forgoes the reliability fix #699 exists to deliver.
   Staging (`.github/workflows/fly-deploy-stage.yml`) has no such gate: it auto-creates/updates
   all three class machines on every push to `main` once its own Desk preflight reports ready.
2. **Phase 1's exit criteria require a live window to measure**, and the only unknowns that
   matter here are the ones that cannot be produced synthetically: real in-progress provider
   payloads (`tests/integration/_desk_replay.py` documents that its only in-progress frame is
   `derived=True` — a real *Scheduled* game manually advanced to `gameStatus == 2`, since no
   genuinely in-progress capture exists anywhere in this repo), `NBAStatsClient` latency/retry
   behavior inside the fast class's seconds-scale budget, Fly's actual fire-time behavior for
   three independently scheduled machines, the fast machine's 512MB sizing under real load, and
   backbone-vs-fast interleaving under real (not replayed) write volume.

## Prerequisites

- `flyctl auth login` (or an existing session) with access to both `draft-app` (staging) and
  `draft-app-prod`.
- Conda env: `conda activate draftguru`, or prefix commands with
  `conda run -n draftguru --no-capture-output`.
- **Staging is non-prod** and shares the `dev` Neon branch your regular `.env` already targets
  — staging commands need no override; use `scripts/with-db-env.sh` directly (it sources
  `.env`).
- **Prod** is the only environment whose `DATABASE_URL` differs. Per
  `docs/plans/summer-league-desk-536-deploy-runbook.md`, put the prod branch's connection
  string in a dedicated, gitignored override file and point `scripts/with-db-env.sh` at it via
  `ENV_FILE`:

  ```bash
  # one-time, do NOT commit this file
  cat > .env.sl-desk-prod <<'EOF'
  DATABASE_URL=postgresql+asyncpg://...prod-neon-branch-connection-string...
  SECRET_KEY=<any non-empty value -- only needed because importing app.config requires it>
  EOF
  ```

  Prod commands below use `ENV_FILE=.env.sl-desk-prod scripts/with-db-env.sh conda run -n
  draftguru --no-capture-output <command>`; staging commands drop the `ENV_FILE=` prefix.
- `unset GH_TOKEN &&` before any `gh workflow run` / `gh issue` call — the env-var `GH_TOKEN`
  in this environment lacks the `project` scope some `gh` subcommands need; the keyring
  credential does not.

## Dates

Vegas Summer League 2026 ran 2026-07-09 through 2026-07-19. The 2027 dates are set by the
league and are not yet published as of this runbook's writing — **pin the T-30d/T-14d dates
below to the actual 2027 schedule once the NBA announces it** (typically several months out).
Until then, treat "T-30d" / "T-14d" as day-counts back from the first scheduled tip, not fixed
calendar dates. The reminder workflow (`.github/workflows/preflight-reminder.yml`) fires
2027-06-01 specifically so this pinning happens with time to spare rather than being discovered
the week of.

---

## T-30d — build-time verification

Owner: whoever is driving the 2027 pre-event pass (this is a one-maintainer repo today — same
person who executes the rest of this runbook).

### 1. Prod deploy currency check — **[READ-ONLY]**

```bash
ENV_FILE=.env.sl-desk-prod scripts/with-db-env.sh conda run -n draftguru --no-capture-output \
  python scripts/check_deploy_freshness.py --app draft-app-prod --against origin/main
```

This is the same check `.github/workflows/deploy-freshness.yml` already runs daily at 13:00
UTC — check that workflow's most recent run in the Actions tab first; only run the command
by hand if you want a fresh read right now. A red result here means prod is running an old
image relative to `main` (the #669 failure mode); redeploy via
`gh workflow run fly-deploy-prod.yml -f ref=main` before doing anything else in this runbook.

Also confirm the `db-health` job in the same workflow is green (it probes
`https://draft-app.fly.dev/health/db` and `https://draft-app-prod.fly.dev/health/db` via
`scripts/check_health_db.py`).

### 2. Roster-cron year configuration — **[READ-ONLY]**

`app/cli/summer_league_roster_runner.py`'s `SL_ROSTER_YEAR` default is "the current Eastern
calendar year" (its `_default_year()` helper), so **no action is required for
a same-calendar-year event** — 2027 Summer League run in 2027 needs no override. Only act if
either is true:

- You need to force a run outside the lifecycle window for a deliberate operator backfill —
  set `SL_ROSTER_FORCE=1` (never combine casually with a live event; it bypasses the window
  gate entirely).
- You need to scope a run to a *different* year than the current calendar year (e.g. a
  same-year late-season backfill of 2027 data run in early 2028) — set
  `SL_ROSTER_YEAR=2027`.

Neither var is set today (`grep -rn "SL_ROSTER_YEAR\|SL_ROSTER_FORCE" deploy/fly/` returns
nothing), which is correct steady state. If an override is ever needed on production, set it
as a Fly app secret so the roster cron machine picks it up on its next invocation:

```bash
# [MUTATES INFRA] -- only if an override is actually needed; see above
flyctl secrets set SL_ROSTER_YEAR=2027 --app draft-app-prod
```

Unset it (`flyctl secrets unset SL_ROSTER_YEAR --app draft-app-prod`) once the reason for the
override has passed — leaving it set silently narrows every future run's window resolution to
that one year.

### 3. Staging replay soak of the class machines — **[READ-ONLY]** + **[MUTATES INFRA: staging]**

Two distinct checks, both required:

**a. The replay harness (off-season, no live games needed):**

```bash
conda run -n draftguru --no-capture-output \
  python -m pytest tests/integration/test_sl_desk_latency_classes.py -q
```

This exercises `tests/integration/_desk_replay.py`, which steps a real captured 2026 live-window
provider payload (`scheduleleaguev2_15_2026_live_pretip.json`) through the partitioned tick
classes while a stand-in backbone holds the writer lock.
`test_fast_class_lands_every_live_window_tick_while_backbone_holds_lock` is the acceptance
signal: it asserts the fast class lands **100% of live-window frames** while a matched set of
tests assert the pre-#699 composite, and the fast class *without* the lock exemption, both fail
under the same contention — the matched pair exists so the pass isn't vacuous. A red run here
means something regressed the partition itself; do not proceed to T-14d until this is green.

**b. Real staging class machines running under real (if quiet) conditions:**

Staging auto-creates and keeps `summer-league-desk-fast-cron`, `summer-league-desk-projection-cron`,
and `summer-league-desk-backbone-cron` on `draft-app` updated on every push to `main` once its
own Desk preflight passes (`.github/workflows/fly-deploy-stage.yml`, "Deploy/update Summer
League Desk latency-class cron machines" step) — they should already exist and have been
running continuously since PR #706 merged, off-season ticks resolving to
`dormant_noop`. Confirm they're actually alive and their pipeline-state rows are advancing:

```bash
scripts/with-db-env.sh conda run -n draftguru --no-capture-output python - <<'EOF'
import asyncio
from sqlalchemy import select
from app.utils.db_async import SessionLocal
from app.schemas.summer_league_pipeline import SummerLeaguePipelineJob, SummerLeaguePipelineState

async def main():
    async with SessionLocal() as db:
        result = await db.execute(
            select(SummerLeaguePipelineState).where(
                SummerLeaguePipelineState.job.in_(
                    [
                        SummerLeaguePipelineJob.DESK_FAST,
                        SummerLeaguePipelineJob.DESK_PROJECTION,
                        SummerLeaguePipelineJob.DESK_BACKBONE,
                    ]
                )
            )
        )
        for row in result.scalars():
            print(row.job, row.last_outcome, row.last_started_at, row.last_completed_at)

asyncio.run(main())
EOF
```

Expect all three jobs present with `last_outcome=succeeded` and `last_completed_at` within the
last ~1h (fast/projection) or ~24h (backbone) — `start_pipeline`/`complete_pipeline`
(`app/cli/_desk_class_runner.py`) record every invocation unconditionally, including
off-window `dormant_noop` runs, so staleness here means the machine itself stopped firing, not
that the event is off-season. If any row is missing or stale, check
`flyctl machine list --app draft-app` for the three machine names above and
`flyctl logs --app draft-app --instance <id>` for the stalled one before continuing.

---

## T-14d — announce horizon: promotion sequence

Owner: same as above. Do not start this until T-30d's three checks are all green.

This is PR #706's documented promotion sequence
(`deploy/fly/fly.cron.desk.fast.toml`), executed for real against production.

### 1. Deploy prod with the class machines gated on — **[MUTATES INFRA]**

```bash
unset GH_TOKEN && gh workflow run fly-deploy-prod.yml -f ref=main -f enable_desk_class_crons=true
```

Watch the run (`gh run watch`). The workflow:
- Runs `scripts/check_sl_desk_readiness.py preflight` against prod (`desk_preflight` step) —
  this must pass or nothing below happens (a red preflight logs
  `::warning::...skipping Desk cron machine creation this deploy` and the run still reports
  green, since preflight failing here is expected pre-launch, not a broken deploy).
- Updates any *existing* class machine images unconditionally (composite Desk cron included).
- **Creates** `summer-league-desk-fast-cron` (512MB, hourly), `summer-league-desk-projection-cron`
  (1024MB, hourly), and `summer-league-desk-backbone-cron` (2048MB, daily) on `draft-app-prod`
  if they don't exist yet — gated on both `enable_desk_class_crons == true` and
  `desk_preflight.outputs.ready == 'true'`.
- Runs `scripts/verify_cron_image_digests.py` post-deploy across all Summer League cron
  machines including the three class machines (as `--optional-machine`, since they may not
  exist pre-promotion) — a drifted-but-present machine fails this check.

### 2. Verify per-class telemetry rows advance in prod — **[READ-ONLY]**

Same query as the T-30d staging check, pointed at prod:

```bash
ENV_FILE=.env.sl-desk-prod scripts/with-db-env.sh conda run -n draftguru --no-capture-output \
  python - <<'EOF'
import asyncio
from sqlalchemy import select
from app.utils.db_async import SessionLocal
from app.schemas.summer_league_pipeline import SummerLeaguePipelineJob, SummerLeaguePipelineState

async def main():
    async with SessionLocal() as db:
        result = await db.execute(
            select(SummerLeaguePipelineState).where(
                SummerLeaguePipelineState.job.in_(
                    [
                        SummerLeaguePipelineJob.DESK_FAST,
                        SummerLeaguePipelineJob.DESK_PROJECTION,
                        SummerLeaguePipelineJob.DESK_BACKBONE,
                    ]
                )
            )
        )
        for row in result.scalars():
            print(row.job, row.last_outcome, row.last_started_at, row.last_completed_at)

asyncio.run(main())
EOF
```

Wait for at least one full cycle of each class (an hour is enough for fast/projection; give the
backbone class up to 24h, or trigger it once manually per the fallback commands in
`docs/plans/summer-league-desk-536-deploy-runbook.md` §2.4, adapted to the class machine name
and `-m app.cli.sl_desk_backbone_tick`) before declaring this step done. All three rows should
show `last_outcome=succeeded`.

### 3. Stop the composite — **[MUTATES INFRA]** — the point of no return

Only after step 2 is green for all three classes:

```bash
# [MUTATES INFRA]
CRON_ID=$(flyctl machine list --app draft-app-prod --json | jq -r '.[] | select(.name == "summer-league-desk-cron") | .id')
flyctl machine stop "$CRON_ID" --app draft-app-prod
```

This is deliberately manual and un-automated — `deploy/fly/fly.cron.desk.fast.toml`'s
PROMOTION comment calls it "step 4. ONLY THEN stop the old composite machine... Do not skip
step 4." Leaving the composite running duplicates work and still takes the shared writer lock,
partly re-creating the starvation #699 removes.

### Rollback

If anything in the promotion looks wrong before or after stopping the composite:

```bash
# [MUTATES INFRA] -- restart the composite
CRON_ID=$(flyctl machine list --app draft-app-prod --json | jq -r '.[] | select(.name == "summer-league-desk-cron") | .id')
flyctl machine start "$CRON_ID" --app draft-app-prod

# [MUTATES INFRA] -- stop the three class machines
for NAME in summer-league-desk-fast-cron summer-league-desk-projection-cron summer-league-desk-backbone-cron; do
  MID=$(flyctl machine list --app draft-app-prod --json | jq -r --arg n "$NAME" '.[] | select(.name == $n) | .id')
  [ -n "$MID" ] && flyctl machine stop "$MID" --app draft-app-prod
done
```

The composite entrypoint (`app.cli.sl_desk_tick`) is unchanged and does all the same work the
three classes do combined — every Desk table is a rebuildable read-model projection, so nothing
else needs to be undone. Re-running the promotion later just repeats steps 1–3.

---

## During event — exit-criteria measurements

Phase 1's exit criteria (`docs/plans/summer-league-remediation-roadmap.md`, "Phase 1 —
Operational: cron and database reliability", Exit paragraph) require **hourly tick completion
measured inside live-game windows, not daily-averaged**, because off-peak ticks succeed easily
and mask exactly the live-window misses that were #699's whole reason for existing
(`docs/plans/summer-league-desk-simplification-spec.md` §"Success metric").

**Where to read it:** `summer_league_pipeline_states`, same query as above, run repeatedly
during a live window (e.g. once per hour while games are in progress):

```bash
ENV_FILE=.env.sl-desk-prod scripts/with-db-env.sh conda run -n draftguru --no-capture-output \
  python scripts/check_sl_desk_readiness.py post-tick
```

`post-tick` mode fails hard if the scheduler check (`_newest_pipeline_state` over `DESK` +
`DESK_PROJECTION` — whichever is currently reporting content freshness, so the check does not
false-fail the moment promotion completes and the composite stops) is stale beyond
`--staleness-hours` (default: twice the scheduled interval, i.e. tolerates exactly one missed
hourly tick), or if the render-snapshot matrix is incomplete. Source freshness is checked
separately over `DESK` + `DESK_FAST` (the fast class is the one that talks to the provider once
promoted). Neither check is per-class-granular by design — cross-reference the raw rows query
above for each class's own `last_outcome`/`last_completed_at` individually.

**What "met" means:** the replay harness's acceptance bar, now verified for real —
`test_fast_class_lands_every_live_window_tick_while_backbone_holds_lock` proved 100% of
scheduled fast-class ticks land with advanced source data *while the backbone class holds the
writer lock*, using a real captured 2026 live-window payload replayed off-season. During the
2027 event, confirm this holds under actual conditions:

- **Fast class**: every scheduled hourly tick during a live-game window shows
  `last_outcome=succeeded` with `last_content_updated=true` for at least the games that were
  actually in progress that hour. A single missed/failed fast tick during a live window is the
  exact regression #699 exists to prevent — treat it as a P0 investigation, not noise.
- **Projection class**: `last_outcome=succeeded` on its hourly cadence; `last_content_updated`
  should track whenever the fast class advanced source data that hour (it rebuilds the Desk
  projection from whatever the fast class just wrote).
- **Backbone class**: `last_outcome=succeeded` on its daily cadence; a single slow/failed
  backbone run during a live window must NOT correlate with a fast-class miss in the same
  window — that decoupling is the entire point of the partition, and a correlated failure means
  the writer-lock isolation broke.
- Also watch `/health/db` (`https://draft-app-prod.fly.dev/health/db`) for pool exhaustion
  under real concurrent write volume — the fast/projection/backbone split reduces lock
  contention but does not change total query volume, and #716's per-class query budgets
  (`tests/integration/perf/budgets.py`: fast 89 / projection 487 / backbone 13 queries per
  tick) are the offline proxy for what this endpoint would show going red.

---

## T+close — wind-down checks

Owner: same as above, executed once the event lifecycle has moved past Active.

### 1. Roster cron goes dormant — **[READ-ONLY]**

`app/services/summer_league/event_window.py`'s `SCHEDULE_ELIGIBLE_PHASES` includes
`ANNOUNCED`, `WARMUP`, `ACTIVE`, and `WINDDOWN` — the roster runner
(`app/cli/summer_league_roster_runner.py`) still polls during Wind-down, and only goes dormant
(exits before opening the roster fetch path) once the lifecycle reaches `ARCHIVED`
(`app/schemas/event_desk.py`'s `EventLifecyclePhase`). Confirm the roster cron's logs show it
exiting early with no roster-fetch activity once the event has clearly archived (no games for
several days past the announced end date):

```bash
CRON_ID=$(flyctl machine list --app draft-app-prod --json | jq -r '.[] | select(.name == "summer-league-roster-cron") | .id')
flyctl logs --app draft-app-prod --instance "$CRON_ID"
```

### 2. Compaction retention — **[READ-ONLY]**

`summer-league-metrics-compact-cron` (`app/cli/summer_league_metrics_compact.py`, daily,
`deploy/fly/fly.cron.metrics-compact.toml`) keeps running unconditionally — it is not
lifecycle-gated, since it only prunes superseded closed-day metric projection rows under the
shared writer lock (`app/services/summer_league/metric_compaction.py`). Confirm it is still
succeeding daily post-event (no special T+close action needed beyond checking its own logs) —
retention is bounded at one published + one abandoned-candidate row per scope per closed UTC
day, so a healthy post-event run should show growth flattening out rather than continuing to
accumulate hourly-rebuild churn once the event stops producing new closed days.

### 3. Class machines after wind-down

No action required. The class machines keep running hourly/hourly/daily year-round exactly as
they did off-season before 2027 (resolving to `dormant_noop` outside any window) — there is
nothing to stop. Only the composite `summer-league-desk-cron`, if it was left running instead
of stopped at T-14d, is worth revisiting: if the 2027 promotion succeeded, it should already be
stopped; if it's still running post-event, that's a leftover from an incomplete promotion, not
a wind-down artifact, and should be investigated before the next event rather than left for
2028.

---

## Related documents

- `docs/plans/summer-league-desk-536-deploy-runbook.md` — the composite Desk cron's original
  (single-machine) launch runbook; this document's Part 2 promotion mechanics are the direct
  ancestor of this runbook's T-14d section.
- `docs/plans/summer-league-desk-simplification-spec.md` §2 — the #699 partition design, success
  metric, and the "three things load-bearing and easy to undo by accident" list this runbook's
  T-14d/During-event sections operationalize.
- `docs/plans/summer-league-remediation-roadmap.md` — Phase 1 status and exit criteria this
  runbook exists to close out.
- `docs/fly_infrastructure.md` — health-endpoint and deploy-freshness posture (#714) referenced
  in the T-30d and During-event sections.
