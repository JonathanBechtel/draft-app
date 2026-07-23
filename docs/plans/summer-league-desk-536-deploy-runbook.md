# Summer League Desk (#536) Deploy Runbook

## Status

Runbook for a **human to execute**, not an agent. Provisioning the Fly cron machine,
running Job A (`scripts/build_sl_cohort_baselines.py`) against a real environment, and
`flyctl machine update`/`flyctl machine run` are real-infrastructure mutations that were
deliberately kept out of the automated ticket work for #536 — see
`docs/plans/summer-league-desk-launch-readiness.md` item 4 ("Desk deployment readiness").

Everything below is read-only-safe to *plan*, but the commands themselves mutate a real
Fly app or a real Neon database branch. Every command that touches real infrastructure is
marked **[MUTATES INFRA]**. Commands marked **[READ-ONLY]** are always safe to re-run.

## What CI already does for you

`.github/workflows/fly-deploy-stage.yml` and `.github/workflows/fly-deploy-prod.yml` (as
of this ticket) already:

- Run `scripts/check_sl_desk_readiness.py preflight` (read-only) after every deploy.
- Idempotently create-if-absent / update-if-present the `summer-league-desk-cron`
  machine **only when preflight passes** (stage: automatically on every push to `main`;
  prod: only when the `enable_desk_cron` workflow-dispatch input is explicitly `true`
  **and** prod's own preflight passes).
- **Never** run Job A. **Never** run the tick itself outside the scheduled machine.

So the only genuinely manual steps below are: (1) building the T1 baseline (Job A), which
must exist *before* CI's preflight will let the machine get created, and (2) starting the
newly-created machine once for its first tick, since it's easier to verify one deliberate
run than to wait up to an hour for the schedule.

## Launch-day data dependency (verified during the 2026-07-12 dry-run)

The Desk renders live content **only when the current slate's games exist with `tip_datetime`**. That value is populated **only** by the scoreboard/schedule ingest (`run_scoreboard_ingest`, the `scheduleleaguev2` feed) — **not** by the hourly box-score cron (`app.cli.summer_league_ingest_runner`), which reads `leaguegamelog` (played games only: no tips, no forward schedule). A DB with only box-score data resolves the Desk to **off-window / dormant** even mid-event.

How the tick self-bootstraps (competition-window + #527 work):

- `SummerLeagueCompetition.starts_on`/`ends_on` are now populated from game dates on every normalize/scoreboard ingest, so the #527 opening-morning bootstrap is no longer inert.
- On a tick within the announce/pre-roll window with no games yet, `run_desk_tick` runs `run_scoreboard_ingest` first, pulls today/tomorrow's games **with tips**, then resolves Active and materializes live content.

**Constraint:** `run_scoreboard_ingest` hits `stats.nba.com`, which needs curl_cffi Chrome impersonation and is **not reachable from every network** (reachable from the Fly cron machine; it was NOT from the local dry-run box). So the first *live* tick must run **on the deployed Fly machine**, not locally.

Practical staging sequence: deploy the cron → its first tick (on Fly) bootstraps the current slate with tips and goes Active → confirm via `post-tick` readiness (full 72-variant matrix + fresh state) and by eyeballing the homepage (should show live Preview/Live/Recap, not the off-window strip).

## Prerequisites

- `flyctl auth login` (or an existing session) with access to both `draft-app` and
  `draft-app-prod`.
- Conda env: `conda activate draftguru` (or prefix commands with
  `conda run -n draftguru --no-capture-output`).
- **Staging is non-prod** — the same Neon branch (`dev`) your regular `.env` already
  targets. Staging commands therefore need **no override**: use `scripts/with-db-env.sh`
  (which sources `.env`) directly.
- **Prod** is the only environment whose `DATABASE_URL` differs. Put the prod branch's
  connection string in a dedicated, **gitignored** override file (never inline it, per
  `feedback_no_inline_env_chains`) and point `scripts/with-db-env.sh` at it via `ENV_FILE`:

  ```bash
  # one-time, do NOT commit this file
  cat > .env.sl-desk-prod <<'EOF'
  DATABASE_URL=postgresql+asyncpg://...prod-neon-branch-connection-string...
  SECRET_KEY=<any non-empty value -- only needed because importing app.config requires it>
  EOF
  ```
  Get the prod branch URL from the Neon console for project `draftguru`, or
  `neonctl connection-string <prod-branch> --project-id <draftguru-project-id>`.

Staging Job A / readiness commands below use `scripts/with-db-env.sh conda run -n draftguru
--no-capture-output <command>` (regular `.env`); prod commands add the
`ENV_FILE=.env.sl-desk-prod` prefix.

---

## Part 1 — Staging

### 1.1 Build the T1 cohort baseline (Job A) — **[MUTATES INFRA: writes to the stage DB]**

Deliberate, one-time (or "rare refresh") step. Safe to re-run — each run writes a fresh
`baseline_version` and flips `is_active`; it never deletes prior versions.

```bash
scripts/with-db-env.sh conda run -n draftguru --no-capture-output \
  python scripts/build_sl_cohort_baselines.py
```

Expected output: `Built Summer League cohort baselines: version=<...> (season_range=2017-2025, min_minutes=..., game_min_minutes=...)`.

### 1.2 Preflight — **[READ-ONLY]**

```bash
scripts/with-db-env.sh conda run -n draftguru --no-capture-output \
  python scripts/check_sl_desk_readiness.py preflight
```

Must print `READY` and exit 0. If it doesn't:
- `registration: FAIL` → this year's `summer_league_competitions` rows aren't seeded yet
  on stage; ingest/seed them before continuing.
- `baselines: FAIL` → step 1.1 didn't produce all three required grains (event, debut,
  game); re-run it and check its printed `season_range`/`min_minutes` args are sane.
- `freshness` / `render_snapshots` will show `SKIP` here — expected pre-tick, not a
  problem.

### 1.3 Deploy — **[MUTATES INFRA]**

Normal path: push to `main` (or re-run the existing `fly-deploy-stage.yml` workflow via
`gh workflow run fly-deploy-stage.yml` / the Actions UI). CI will:
- Deploy the app image.
- Update the news/SL-ingestion/roster cron machines (existing behavior, unchanged).
- Re-run the same preflight check from 1.2 against stage.
- Since it now passes, **create** the `summer-league-desk-cron` machine (it doesn't
  exist yet) from `deploy/fly/fly.cron.desk.stage.toml`'s process command, on an hourly schedule.

Watch the workflow run (`gh run watch` or the Actions UI) and confirm the
"Deploy/update Summer League Desk cron machine (preflight-gated)" step actually created
the machine (its log line reads `No Desk cron machine found; creating one...`).

If you'd rather do this by hand instead of waiting on CI (e.g. CI is down):

```bash
# [MUTATES INFRA]
flyctl deploy --config deploy/fly/fly.toml --app draft-app
IMAGE=$(flyctl machine list --app draft-app --json | jq -r '[.[] | select(.config.metadata.fly_process_group == "app")] | first | .config.image')
flyctl machine run "$IMAGE" \
  --app draft-app \
  --schedule hourly \
  --name summer-league-desk-cron \
  --region ewr \
  --memory 1024 \
  --cpus 1 \
  --entrypoint "/app/.venv/bin/python" \
  -- scripts/sl_desk_tick.py
```

### 1.4 Manual first tick — **[MUTATES INFRA: writes T2/T3/T4/event_desk_state on stage]**

Don't wait up to an hour for the schedule; start the machine once, immediately, so you
can verify the very first tick succeeds:

```bash
# [MUTATES INFRA]
CRON_ID=$(flyctl machine list --app draft-app --json | jq -r '.[] | select(.name == "summer-league-desk-cron") | .id')
flyctl machine start "$CRON_ID" --app draft-app
flyctl logs --app draft-app --instance "$CRON_ID"
```

Confirm the logs show a `Summer League Desk tick @ ...` summary line with no traceback
(a dormant/off-window tick printing `off-window (dormant) -- no-op` is a *successful*
run, not a failure — it just means today isn't inside the SL calendar window).

### 1.5 Post-tick smoke check — **[READ-ONLY]**

```bash
scripts/with-db-env.sh conda run -n draftguru --no-capture-output \
  python scripts/check_sl_desk_readiness.py post-tick
```

Must print `READY` and exit 0. This is the smoke check confirming **both** an active
baseline **and** a Desk machine (via a fresh `event_desk_state`) exist — the two things
this whole ticket exists to guarantee before promotion. If `registration` or `freshness`
still fails here, the tick in 1.4 didn't actually run/commit — check its logs again
before proceeding.

---

## Part 2 — Promote to production

Only proceed once **Part 1 is fully green** (1.5 exits 0). This is the "successful
staging evidence" gate the prod workflow's `enable_desk_cron` input encodes.

### 2.1 Build the T1 cohort baseline (Job A) on prod — **[MUTATES INFRA: writes to the prod DB]**

```bash
ENV_FILE=.env.sl-desk-prod scripts/with-db-env.sh conda run -n draftguru --no-capture-output \
  python scripts/build_sl_cohort_baselines.py
```

### 2.2 Preflight against prod — **[READ-ONLY]**

```bash
ENV_FILE=.env.sl-desk-prod scripts/with-db-env.sh conda run -n draftguru --no-capture-output \
  python scripts/check_sl_desk_readiness.py preflight
```

Must exit 0 before continuing (same failure modes as 1.2).

### 2.3 Deploy with the Desk cron enabled — **[MUTATES INFRA]**

Trigger `fly-deploy-prod.yml` with `enable_desk_cron: true`:

```bash
unset GH_TOKEN && gh workflow run fly-deploy-prod.yml -f ref=main -f enable_desk_cron=true
```

(or via the Actions UI: "Run workflow" → set `enable_desk_cron` to `true`). CI will
deploy the app, re-run preflight against prod, and — since both the input and preflight
now hold — create the `summer-league-desk-cron` machine on `draft-app-prod`.

Manual fallback if CI is unavailable:

```bash
# [MUTATES INFRA]
flyctl deploy --config deploy/fly/fly.prod.toml --remote-only --app draft-app-prod
IMAGE=$(flyctl machine list --app draft-app-prod --json | jq -r '[.[] | select(.config.metadata.fly_process_group == "app")] | first | .config.image')
flyctl machine run "$IMAGE" \
  --app draft-app-prod \
  --schedule hourly \
  --name summer-league-desk-cron \
  --region ewr \
  --memory 1024 \
  --cpus 1 \
  --entrypoint "/app/.venv/bin/python" \
  -- scripts/sl_desk_tick.py
```

### 2.4 Manual first tick on prod — **[MUTATES INFRA: writes T2/T3/T4/event_desk_state on prod]**

```bash
# [MUTATES INFRA]
CRON_ID=$(flyctl machine list --app draft-app-prod --json | jq -r '.[] | select(.name == "summer-league-desk-cron") | .id')
flyctl machine start "$CRON_ID" --app draft-app-prod
flyctl logs --app draft-app-prod --instance "$CRON_ID"
```

### 2.5 Post-tick smoke check on prod — **[READ-ONLY]**

```bash
ENV_FILE=.env.sl-desk-prod scripts/with-db-env.sh conda run -n draftguru --no-capture-output \
  python scripts/check_sl_desk_readiness.py post-tick
```

Must print `READY` and exit 0. Once this is green, visit https://draft-app-prod.fly.dev
and confirm the Desk actually renders on the homepage (or wherever the Event Desk
controller currently mounts it).

The readiness digest deliberately separates scheduler execution from useful work:

- `scheduler` reports the scheduled image, last start/completion/outcome, and
  `content_updated` flag.
- `source_freshness` reports the last successful source observation and the last
  run where source rows actually advanced.
- `freshness` reads only `event_desk_state.content_refreshed_at`, the successful
  projection watermark.
- `render_snapshots` reports the newest snapshot watermark and requires the full
  variant matrix only while the event lifecycle is Active or Wind-down.

A dormant/off-window run is healthy when `scheduler` passes with
`content_updated=false`; source, projection, and snapshot checks report intentional
inactivity. It must not advance any of those three content watermarks. During an
active window, all three watermarks must be within `--staleness-hours`, and the
scheduled machine image must still be verified with
`scripts/verify_cron_image_digests.py`.

---

## Rollback

Stopping the machine halts future ticks immediately; `event_desk_state` simply stops
refreshing (its `content_refreshed_at` will go stale, which the existing UI freshness
handling is expected to render honestly — nothing else needs to be undone, since every
Desk table is a rebuildable read-model projection, never a source of truth).

```bash
# [MUTATES INFRA] -- stage
CRON_ID=$(flyctl machine list --app draft-app --json | jq -r '.[] | select(.name == "summer-league-desk-cron") | .id')
flyctl machine stop "$CRON_ID" --app draft-app

# [MUTATES INFRA] -- prod
CRON_ID=$(flyctl machine list --app draft-app-prod --json | jq -r '.[] | select(.name == "summer-league-desk-cron") | .id')
flyctl machine stop "$CRON_ID" --app draft-app-prod
```

To fully remove instead of just pausing:

```bash
# [MUTATES INFRA]
flyctl machine destroy "$CRON_ID" --app <draft-app|draft-app-prod> --force
```

To resume after a stop, `flyctl machine start "$CRON_ID" --app <app>` — no need to
re-run Job A or re-create the machine; the next scheduled/manual tick picks up exactly
where the read-model left off (every write in the tick is an idempotent upsert).

---

## Ongoing operations (after initial launch)

- **Every subsequent deploy** waits for the existing machine to reach `stopped` before
  refreshing its image with `flyctl machine update --skip-start` on both stage and
  prod — no new machine, no Job A re-run, and no interrupted tick. A 30-minute wait
  timeout warns and skips that deploy's image update. `enable_desk_cron` on prod only
  gates *creating* the machine the first time.
- **Re-run Job A** only when the underlying history changes meaningfully (new season's
  data folded into 2017-2025 history, a window-rule change, or a manual data-quality
  fix) — never as part of a routine deploy. Re-running is always safe (writes a new
  `baseline_version`, flips `is_active`, never touches prior versions).
- **If the machine's image goes stale** (e.g. a manual `flyctl deploy` was run without
  going through CI), re-run the "Deploy/update Summer League Desk cron machine" logic by
  hand — the snippet in 1.3/2.3 handles both create and update cases.
