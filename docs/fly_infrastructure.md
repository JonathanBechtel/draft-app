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

- **Purpose**: Run the Summer League Desk hourly tick (`scripts/sl_desk_tick.py`):
  scoreboard ingest -> targeted live raw refresh -> normalize -> scoped metrics
  rebuild -> grades -> storylines -> commentary -> `event_desk_state` upsert.
- **Resources**: 1 shared CPU, 1GB RAM
- **Entrypoint**: `/app/.venv/bin/python scripts/sl_desk_tick.py`
- **Region**: `ewr` (Newark)
- **Schedule**: Hourly in both environments -- not just a freshness preference, a
  correctness requirement. `app/services/event_desk/controller.py` hardcodes
  `TICK_INTERVAL = timedelta(hours=1)` and the Desk UI displays `next_tick_eta`
  derived from it; any other cadence would make that freshness promise false.
- **Config**: `fly.cron.desk.stage.toml` (stage) / `fly.cron.desk.toml` (prod).

Unlike every other cron above, this machine's create-and-update is **readiness-gated**
and Job A is **never run automatically**:

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
- **Staging**: `.github/workflows/fly-deploy-stage.yml` runs the preflight check on every
  push-to-`main` deploy; only when it passes does the workflow idempotently
  create-if-absent / update-if-present the `summer-league-desk-cron` machine. A failed
  preflight skips that step without failing the deploy.
- **Production**: `.github/workflows/fly-deploy-prod.yml` is double-gated -- the
  `enable_desk_cron` `workflow_dispatch` input must be explicitly set `true` (a human
  attesting staging already proved a successful tick) **and** production's own preflight
  must pass, or the create/update step is skipped.
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
  6. Only when `enable_desk_cron` is `true`: run `scripts/check_sl_desk_readiness.py
     preflight` against prod (read-only); if it also passes, idempotently
     create-if-absent / update-if-present the Summer League Desk cron machine. Both
     the input and the preflight must hold, or this step is skipped entirely.

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

```bash
# Get current app image
IMAGE=$(flyctl machine list --app draft-app-prod --json | jq -r '[.[] | select(.config.metadata.fly_process_group == "app")] | first | .config.image')

# Get cron machine ID
CRON_ID=$(flyctl machine list --app draft-app-prod --json | jq -r '.[] | select(.name == "news-ingestion-cron") | .id')

# Update cron machine with new image
flyctl machine update $CRON_ID --app draft-app-prod --image $IMAGE --yes
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
