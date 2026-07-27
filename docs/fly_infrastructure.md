# Fly.io Infrastructure

DraftGuru runs on Fly.io with separate staging and production environments. This document describes the infrastructure architecture, deployment workflows, and operational commands.

---

## Apps Overview

| App Name | Environment | URL | Deployment |
|----------|-------------|-----|------------|
| `draft-app` | Staging | https://draft-app.fly.dev | Auto on push to `main` |
| `draft-app-prod` | Production | https://draft-app-prod.fly.dev | Manual workflow dispatch |

---

## Machine Types

Each app runs two types of machines:

### Web Application Machines

- **Purpose**: Serve the FastAPI application
- **Port**: 8080 (internal), HTTPS exposed externally
- **Resources**: 1 shared CPU, 1GB RAM
- **Region**: `ewr` (Newark)
- **Release command**: Runs `alembic upgrade head` on deploy

**Staging-specific:**
- Auto-stop disabled (stays running)
- Min machines: 0

**Production-specific:**
- Auto-stop enabled (stops when idle, resumes on request)
- Min machines: 1

#### Health endpoints — liveness vs. readiness

Two endpoints, and pointing the wrong thing at the wrong one makes outages worse:

| Endpoint | Touches DB? | Point this at | Meaning |
|---|---|---|---|
| `/health` | **No** | Fly `[[http_service.checks]]`, machine restart policies | The process is up and serving |
| `/health/db` | Yes (bounded `SELECT 1`) | External uptime monitoring / alerting | This instance can actually get a working database connection |

**Why `/health` must stay database-free.** It is what an orchestrator restarts a machine
on. If it went red during a database outage, Fly would cycle every web machine — turning
a database problem into a database problem *plus* no running app.

**Why `/health/db` must not be a Fly health check** for the same reason: a shared-database
outage marks every machine unhealthy simultaneously, which accomplishes nothing (there is
no healthy machine to shift traffic to) while risking machine churn. Its job is to tell a
*human or pager* that reads are failing.

`/health/db` returns 200 with pool gauges, or **503** with an `error` field and those same
gauges. It is bounded at 5s — deliberately under SQLAlchemy's 30s `pool_timeout` — so a
saturated pool reports fast rather than hanging the probe.

**Currently nothing polls it.** This is the known gap: incident #669 ran ~96 minutes with
`/health` green and public routes 500ing, and until an external monitor watches
`/health/db`, the signal exists but no one is listening. Wiring that monitor is tracked
with the deploy-freshness work in the Phase 1 roadmap.

### Cron Machines (news-ingestion-cron)

- **Purpose**: Run scheduled news feed ingestion
- **Resources**: 1 shared CPU, 512MB RAM
- **Entrypoint**: `/app/.venv/bin/python -m app.cli.cron_runner`
- **Region**: `ewr` (Newark)

| Environment | Schedule |
|-------------|----------|
| Staging | Daily |
| Production | Hourly |

The cron runner (`app/cli/cron_runner.py`) executes the news ingestion service, logs progress, and exits cleanly.

### Cron Machine (summer-league-desk-cron)

- **Purpose**: Run the Summer League Desk hourly tick (`app/cli/sl_desk_tick.py`):
  scoreboard ingest -> targeted live raw refresh -> normalize -> scoped metrics
  rebuild -> grades -> storylines -> commentary -> `event_desk_state` upsert.
- **Resources**: 1 shared CPU, 1GB RAM
- **Entrypoint**: `/app/.venv/bin/python -m app.cli.sl_desk_tick`
- **Region**: `ewr` (Newark)
- **Writer priority**: the Desk takes a transaction-scoped PostgreSQL advisory lock
  before its pipeline starts. The full Summer League ingestion cron uses the same
  lock non-blockingly and skips overlapping DB phases, preventing cross-cron
  normalization deadlocks while leaving raw downloads independent.
- **Schedule**: Hourly in both environments -- not just a freshness preference, a
  correctness requirement. `app/services/event_desk/controller.py` hardcodes
  `TICK_INTERVAL = timedelta(hours=1)` and the Desk UI displays `next_tick_eta`
  derived from it; any other cadence would make that freshness promise false.
- **Config**: `fly.cron.desk.stage.toml` (stage) / `fly.cron.desk.toml` (prod).

### Summer League writer coordination and recovery

The Desk and full-ingestion cron are separate hourly machines which share
normalized Summer League tables and derived Desk projections.  NBA/provider
fetches must always run outside a database transaction and outside the shared
PostgreSQL writer lock.  Only the short normalized-write and derivative phases
are serialized.

- The Desk is the higher-priority writer: it waits for the lock, then refreshes
  the active event's scoped metrics and render snapshots.
- The full ingestion job is lower priority: it never waits for the lock.  A
  contended venue, team-box retry, or metrics/snapshot phase records durable
  deferred reconciliation state in `summer_league_pipeline_states` and exits
  safely.
- The next full scheduled run checks that state.  Even with no newly discovered
  games, it must acquire the lock, rebuild metrics, materialize snapshots, and
  clear the deferral only after those steps complete in order.
- Each cron emits `summer_league_pipeline_step` records with `job`, `run_id`,
  `step`, `outcome`, and `duration_ms`, followed by a
  `summer_league_pipeline_run` summary.  These records identify whether delay
  was in provider fetch, lock acquisition, normalization, metrics, Desk
  projections, or snapshots.
- Desk pipeline state distinguishes scheduler health (`last_started_at`,
  `last_completed_at`, `last_outcome`, `last_job_image`) from source observation,
  actual source advance, projection refresh, snapshot materialization, and the
  per-run `last_content_updated` outcome. A successful dormant run updates only
  scheduler completion and records `last_content_updated=false`; it does not move
  content watermarks.

For an incident, inspect the full-ingestion state row before forcing a rerun.
`pending_reconciliation=true` means the next successful full run must catch up;
do not clear it manually.  A state row with repeated `consecutive_deferrals`, a
recent `last_failure_at`, or stale `last_metrics_rebuilt_at` /
`last_snapshots_materialized_at` is the operational signal consumed by the
notification work tracked in #600.

Unlike every other cron above, this machine's **creation** is **readiness-gated** and
Job A is **never run automatically**. Once the machine exists, deploys keep its image
current, but only after `flyctl machine wait --state stopped` confirms no tick is in
flight. A deploy never terminates a Desk tick to refresh its image.

- **Readiness check** (`scripts/check_sl_desk_readiness.py`, read-only, never writes) has
  two modes: `preflight` (run before creating/enabling the machine -- confirms this
  year's Summer League competition(s) are registered and Job A produced an active T1
  cohort baseline for every required grain: event, debut, game) and `post-tick` (run
  right after a deliberate manual first tick -- additionally confirms the `events` row
  synced and `event_desk_state` carries a recent freshness stamp, and validates any
  materialized render snapshots' `schema_version`).
- **Job A** (`scripts/build_sl_cohort_baselines.py`) is the rare, offline T1
  cohort-baseline builder. It is a deliberate, one-time human step run directly against
  an environment's database -- CI never invokes it, and the hourly tick itself raises
  loudly if no active baseline exists rather than building one.
- **Staging**: `.github/workflows/fly-deploy-stage.yml` waits for an existing
  `summer-league-desk-cron` machine to stop before updating its image on a
  push-to-`main` deploy; it runs the preflight check separately, and only when it
  passes does the workflow idempotently create the machine the first time. A failed
  preflight skips creation without failing the deploy.
- **Production**: `.github/workflows/fly-deploy-prod.yml` likewise waits for an
  existing Desk tick to stop before updating the cron image. If the machine does not
  stop within 30 minutes, the workflow logs a warning and skips that image update
  instead of sending `SIGINT` to live work -- but that is no longer the end of the
  story (see "Cron image reconciliation and drift verification" below): a follow-up
  retry gets a second, shorter chance to land the update once the tick finishes, and
  a post-deploy check fails the workflow outright if any Summer League cron machine
  is still on a stale image after that. *Creating* the machine the first time is
  double-gated -- the `enable_desk_cron` `workflow_dispatch` input must be explicitly
  set `true` (a human attesting staging already proved a successful tick) **and**
  production's own preflight must pass, or the create step is skipped. This means a
  routine prod deploy (default `enable_desk_cron=false`) still keeps an
  already-created Desk cron machine's code in sync -- the gate no longer has to be
  re-asserted on every future deploy, only for the original promotion.
- **Cron image reconciliation and drift verification** (spec:
  `docs/plans/summer-league-cron-desk-starvation-spec.md`, proposed work #5): the
  30-minute stop-wait above is a best-effort first attempt, not the last word.
  - **Reconciliation retry** (`scripts/reconcile_cron_image.py`, invoked from the
    "Reconcile Summer League Desk cron machine (retry after stop-wait timeout)" step,
    conditioned on the prior step's `timed_out` output): a bounded 10-minute
    second-chance wait for the Desk cron machine to reach `stopped`, followed by the
    same `machine update --skip-start` + `machine start` sequence the original step
    uses (including the re-arm-the-schedule `machine start` call -- required because
    `machine update` does not reliably re-arm Fly's schedule for a machine left
    stopped). A no-op (`ALREADY_CURRENT`) if the machine already matches, and
    non-fatal (`MACHINE_NOT_FOUND`) if the machine doesn't exist at all -- only a
    still-running machine after the retry window, or a failed `update`/`start` call,
    counts as unreconciled, and that state is caught by the check below rather than
    retried indefinitely within this job.
  - **Post-deploy verification** (`scripts/verify_cron_image_digests.py`, invoked
    from the "Verify Summer League cron image digests (post-deploy)" step, the last
    step in the job): lists every named Summer League cron machine
    (`summer-league-ingestion-cron`, `summer-league-desk-cron`,
    `summer-league-roster-cron`), compares each one's current `config.image` against
    the just-deployed app image, and **fails the workflow** (`::error::`, non-zero
    exit) if any machine that exists is on a different image. `summer-league-desk-cron`
    is passed with `--optional-machine` so its total *absence* (never promoted to this
    environment) doesn't fail the check, but drift on an *existing* Desk cron machine
    fails exactly like the other two -- there is no more silent warning-only skip for
    image drift. Both scripts are plain read-only-except-for-flyctl Python, callable
    the same way from CI or by a human operator locally, following the same shape as
    `scripts/check_sl_desk_readiness.py`.
- **Full runbook** (baseline build, manual first tick, smoke check, stage-to-prod
  promotion, rollback): `docs/plans/summer-league-desk-536-deploy-runbook.md`.

---

## Configuration Files

All Fly config files live under `deploy/fly/` (not the repo root). Every `flyctl`
invocation below assumes the repo root as the working directory, with `--config
deploy/fly/<file>.toml` pointing at the relevant file.

| File | Purpose |
|------|---------|
| `deploy/fly/fly.toml` | Staging web app configuration |
| `deploy/fly/fly.prod.toml` | Production web app configuration |
| `deploy/fly/fly.cron.stage.toml` | Staging news-ingestion cron machine configuration |
| `deploy/fly/fly.cron.toml` | Production news-ingestion cron machine configuration |
| `deploy/fly/fly.cron.sl.stage.toml` / `deploy/fly/fly.cron.sl.toml` | Staging / production Summer League ingestion cron |
| `deploy/fly/fly.cron.roster.stage.toml` / `deploy/fly/fly.cron.roster.toml` | Staging / production Summer League roster cron |
| `deploy/fly/fly.cron.desk.stage.toml` / `deploy/fly/fly.cron.desk.toml` | Staging / production Summer League Desk cron (readiness-gated -- see below) |

---

## CI/CD Workflows

### Staging Deploy (`.github/workflows/fly-deploy-stage.yml`)

- **Trigger**: Automatic on push to `main` branch (also supports manual dispatch)
- **Concurrency**: One deploy at a time; cancels in-progress deploys
- **Steps**:
  1. Checkout code
  2. Run Alembic migrations
  3. Deploy via `flyctl deploy --remote-only`
  4. Set secrets (DATABASE_URL, SECRET_KEY, ENV=stage, etc.)
  5. Update news/Summer-League-ingestion/roster cron machines with latest app image
  6. Run `scripts/check_sl_desk_readiness.py preflight` (read-only); if it passes,
     idempotently create-if-absent / update-if-present the Summer League Desk cron
     machine. A failed preflight skips this step without failing the deploy.

### Production Deploy (`.github/workflows/fly-deploy-prod.yml`)

- **Trigger**: Manual workflow dispatch only
- **Input**: Optional git ref (SHA, tag, or branch) - defaults to `main`; optional
  `enable_desk_cron` boolean (default `false`) - see below
- **Concurrency**: One deploy at a time; does NOT cancel in-progress
- **Steps**:
  1. Checkout specified ref
  2. Run Alembic migrations
  3. Set secrets on prod app
  4. Deploy via `flyctl deploy --config deploy/fly/fly.prod.toml --remote-only --app draft-app-prod`
  5. Update news/Summer-League-ingestion/roster cron machines with latest app image
  6. Update the Summer League Desk cron machine's image too, unconditionally, if it
     already exists (same as step 5) -- this does not depend on `enable_desk_cron`. If
     the machine doesn't stop within 30 minutes, this step warns and skips instead of
     interrupting live work.
  6a. Reconcile: if step 6 timed out, `scripts/reconcile_cron_image.py` gets a bounded
      10-minute second chance to wait-then-update-then-restart the Desk cron machine
      once its in-flight tick finishes.
  7. Only when `enable_desk_cron` is `true`: run `scripts/check_sl_desk_readiness.py
     preflight` against prod (read-only); if it also passes, create the Summer League
     Desk cron machine (idempotent create-if-absent -- a no-op if step 6 already found
     it). Both the input and the preflight must hold, or this creation step is skipped
     entirely; an already-existing machine's image was still refreshed in step 6
     regardless.
  8. Verify: `scripts/verify_cron_image_digests.py` lists every Summer League cron
     machine and **fails the workflow** if any existing machine's image doesn't match
     the just-deployed app image -- the closing net if steps 6/6a couldn't land the
     Desk cron update.

### Review Apps (`.github/workflows/fly-deploy-review.yml`)

- **Trigger**: PR `opened` / `reopened` / `synchronize` / `closed`
- **Per-PR database**: each PR gets its own ephemeral Neon branch (`pr-<number>`)
  forked from the `production` branch via `neondatabase/create-branch-action`.
  (Production is the only branch whose alembic state stays consistent with its
  schema; `development` carries drift from the old shared-review-DB era.)
  The branch's connection string is converted to an asyncpg URL and passed to
  the review app as `DATABASE_URL`, so each preview migrates its own isolated
  database. On PR close, `neondatabase/delete-branch-action` tears the branch
  down (the Fly review app is destroyed by the deploy action on the same event).
- **Why**: review apps previously shared one database. A migration introduced on
  one PR's branch left the shared DB at a revision other PRs couldn't
  `alembic upgrade head` past, so unrelated PRs failed the deploy. Per-PR
  branches eliminate the collision.
- **Required secret**: `NEON_API_KEY` must be available to the `draft-app-dev`
  environment (Neon → project `lingering-tree-42020349` → personal API key).
  Without it the Create/Delete Neon branch steps fail.

---

## Cron Machine Management

Cron machines share the same Docker image as the main app. After each deploy, CI/CD explicitly updates cron machines with the latest image to ensure they run current code.

### Manual Cron Setup (if needed)

```bash
# Step 1: Deploy main app first
flyctl deploy --config deploy/fly/fly.prod.toml --app draft-app-prod

# Step 2: Extract app image and create cron machine
IMAGE=$(flyctl machine list --app draft-app-prod --json | jq -r '[.[] | select(.config.metadata.fly_process_group == "app")] | first | .config.image')
flyctl machine run $IMAGE \
  --app draft-app-prod \
  --schedule hourly \
  --name news-ingestion-cron \
  --region ewr \
  --memory 512 \
  --cpus 1 \
  --entrypoint "/app/.venv/bin/python" \
  -- -m app.cli.cron_runner
```

### Updating Cron Machine Image

**Always pass `--command` alongside `--image`.** A machine's argv is frozen when
`flyctl machine run` created it, so an image-only update can land a new image on
a machine still pointing at an entrypoint that image no longer contains. The cron
then dies every tick while `verify_cron_image_digests.py` stays green, because the
*image* is current — this is exactly how #685 broke the stage Desk cron behind a
fully successful deploy. The deploy workflows re-declare the command on every
update for this reason; manual updates must too.

```bash
# Get current app image
IMAGE=$(flyctl machine list --app draft-app-prod --json | jq -r '[.[] | select(.config.metadata.fly_process_group == "app")] | first | .config.image')

# Get cron machine ID
CRON_ID=$(flyctl machine list --app draft-app-prod --json | jq -r '.[] | select(.name == "news-ingestion-cron") | .id')

# Update cron machine with the new image AND its declared command
flyctl machine update $CRON_ID --app draft-app-prod \
  --image $IMAGE \
  --command "-m app.cli.cron_runner" \
  --yes
```

The command per machine, matching the `cron =` lines in `deploy/fly/*.toml`:

| Machine | `--command` |
|---|---|
| `news-ingestion-cron` | `-m app.cli.cron_runner` |
| `summer-league-ingestion-cron` | `-m app.cli.summer_league_ingest_runner` |
| `summer-league-roster-cron` | `-m app.cli.summer_league_roster_runner` |
| `summer-league-desk-cron` | `-m app.cli.sl_desk_tick` |

Verify afterwards — a green `machine update` does not mean the cron runs:

```bash
flyctl machine list --app draft-app-prod --json \
  | jq -r '.[] | select(.config.schedule) | {name, schedule: .config.schedule, cmd: .config.init.cmd}'
```

---

## Operational Commands

### Viewing Logs

```bash
# Production logs
flyctl logs --app draft-app-prod

# Staging logs
flyctl logs --app draft-app

# Specific machine logs
flyctl logs --app draft-app-prod --instance <machine-id>
```

### Machine Management

```bash
# List all machines
flyctl machine list --app draft-app-prod

# Check machine status
flyctl machine status <machine-id> --app draft-app-prod

# Start a stopped machine
flyctl machine start <machine-id> --app draft-app-prod

# Stop a running machine
flyctl machine stop <machine-id> --app draft-app-prod
```

### Secrets Management

```bash
# List secrets (names only)
flyctl secrets list --app draft-app-prod

# Set a secret
flyctl secrets set KEY=value --app draft-app-prod
```

---

## Environment Variables

Required secrets set on all environments:

| Secret | Description |
|--------|-------------|
| `DATABASE_URL` | PostgreSQL async connection string (asyncpg driver) |
| `SECRET_KEY` | Application secret for authentication/sessions |
| `ENV` | Environment identifier (`dev`, `stage`, `prod`) |
| `LOG_LEVEL` | Logging level (default: `INFO`) |
| `ACCESS_LOG` | Enable/disable HTTP access logs |

Optional secrets:

| Secret | Description |
|--------|-------------|
| `GEMINI_API_KEY` | Gemini API for image generation |
| `GEMINI_SUMMARIZATION_API_KEY` | Separate key for RSS summarization |
| `S3_ACCESS_KEY_ID` | S3 credentials for image storage |
| `S3_SECRET_ACCESS_KEY` | S3 credentials for image storage |
