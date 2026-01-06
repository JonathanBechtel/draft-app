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

---

## Configuration Files

| File | Purpose |
|------|---------|
| `fly.toml` | Staging web app configuration |
| `fly.prod.toml` | Production web app configuration |
| `fly.cron.stage.toml` | Staging cron machine configuration |
| `fly.cron.toml` | Production cron machine configuration |

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
  5. Update cron machine with latest app image

### Production Deploy (`.github/workflows/fly-deploy-prod.yml`)

- **Trigger**: Manual workflow dispatch only
- **Input**: Optional git ref (SHA, tag, or branch) - defaults to `main`
- **Concurrency**: One deploy at a time; does NOT cancel in-progress
- **Steps**:
  1. Checkout specified ref
  2. Run Alembic migrations
  3. Set secrets on prod app
  4. Deploy via `flyctl deploy --config fly.prod.toml --remote-only --app draft-app-prod`
  5. Update cron machine with latest app image

---

## Cron Machine Management

Cron machines share the same Docker image as the main app. After each deploy, CI/CD explicitly updates cron machines with the latest image to ensure they run current code.

### Manual Cron Setup (if needed)

```bash
# Step 1: Deploy main app first
flyctl deploy --config fly.prod.toml --app draft-app-prod

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
