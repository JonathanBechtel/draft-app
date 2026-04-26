# Top 100 Prospect Refresh Workflow

This workflow refreshes the DraftGuru 2026 Top 100 prospect universe from a
frozen board snapshot through dev review and production promotion. It is
designed to keep source capture, code/schema changes, and data mutations
separate.

## Session Order

1. Foundations: freeze the Top 100 source board, resolve affiliations, create a
   player resolution plan, and document the workflow.
2. Dev dedup and resolver hardening: dry-run and apply reviewed duplicate
   merges in dev only, then harden shared resolver code.
3. Dev enrichment: fill canonical identity, bio/status, and current stats in
   dev with provenance.
4. Images: collect reviewed likeness reference-image candidates.
5. Dev QA and fixes: run full QA gates and fix any dev-only issues.
6. Production promotion: run reviewed scripts against prod after a prod dry run.

## Source Snapshot

Run:

```bash
conda run -n draftguru python scripts/top100_refresh.py --date YYYY-MM-DD
```

Expected Session 1 artifacts:

- `scraper/output/top100_source_snapshot_YYYY-MM-DD.csv`
- `scraper/output/school_resolution_review_YYYY-MM-DD.csv`
- `scraper/output/player_resolution_plan_YYYY-MM-DD.csv`
- `scraper/output/top100_refresh_run_note_YYYY-MM-DD.md`

The source snapshot is immutable for a run. If a board changes, generate a new
dated snapshot instead of editing an old one.

## Source Policy

- Use one complete, reviewable Top 100 board as the primary ranking source.
- Preserve source spelling for player names, affiliations, positions, height,
  age, source URL, and source publication date.
- Use Basketball Reference as the preferred player identity, bio, and stats
  source whenever a prospect has an available BBRef page.
- For prospects without BBRef pages, prefer official school/team roster pages,
  official league/team pages, or other reviewed authoritative sources.
- Secondary boards can inform review notes but must not silently override the
  frozen primary snapshot.

## Affiliation Resolution

Use `scripts/data/school_mapping.json` and `scripts/data/college_schools.json`
before any player creation or update.

Rules:

- Preserve the raw source affiliation in `school_raw` or equivalent review
  artifacts.
- Resolve NCAA aliases to canonical school names before matching players.
- Map known variants consistently, including `North Carolina` to `UNC` and
  `Connecticut` to `UConn`.
- Represent professional and international clubs intentionally as
  non-college affiliations. Do not force them into `college_schools`.
- Any unmapped affiliation must be flagged in the review CSV and must not
  proceed to player creation until reviewed.

## Player Resolution

Run the artifact generator with `DATABASE_URL` set when a dev database is
available:

```bash
DATABASE_URL=postgresql+asyncpg://... conda run -n draftguru python scripts/top100_refresh.py --date YYYY-MM-DD
```

The player resolution plan must assign every row one of:

- `matched`
- `merge_required`
- `create_stub`
- `needs_manual_review`

Match using exact names, aliases, suffix-insensitive normalized names, canonical
affiliation, draft year, and available external IDs. Do not create a stub when a
plausible near-match exists. Suffix variants such as `Darius Acuff Jr.` and
`Darius Acuff` must resolve to the same normalized identity for review.

## Dev Data Work

Dev is the only environment for discovery and destructive validation.

- Run merge scripts in dry-run mode first.
- Preserve discarded display names as aliases.
- Move child records before deleting duplicate player records.
- Keep raw source values and provenance fields.
- Do not write raw school/club values directly into canonical fields.

## Code vs Data Migrations

Code/schema migrations and data migrations are separate deliverables.

- Schema changes belong in Alembic revisions and must be reviewed independently.
- Data migrations must be repeatable scripts with dry-run output, reviewed input
  artifacts, and saved execution logs.
- A data migration must state the target environment, expected affected row
  counts, rollback strategy, and verification query.

## QA Gates

Before production promotion:

- 100 source rows exist in the frozen snapshot.
- Every source row has rank, source name, raw affiliation, source URL, and
  source publication date.
- Every raw affiliation is mapped or explicitly flagged for review.
- Every source row has a player resolution action.
- No unresolved duplicate Top 100 identities remain in dev.
- Missing stats, bios, or images have documented reasons.
- No known bad image URLs are queued for image generation.

For code changes, run the repo Definition of Done:

```bash
conda run -n draftguru make precommit
conda run -n draftguru mypy app --ignore-missing-imports
conda run -n draftguru pytest tests/unit -q
```

Run integration tests when touching DB/routes:

```bash
PYTEST_ALLOW_DB=1 TEST_DATABASE_URL=postgresql+asyncpg://... \
  conda run -n draftguru pytest tests/integration -q
```

Run visual checks only for UI changes:

```bash
conda run -n draftguru make visual
```

## Production Promotion

Production promotion is a separate session.

1. Confirm reviewed artifacts and dev QA logs are attached or committed.
2. Take a backup or confirm provider point-in-time recovery coverage.
3. Run the production data migration in dry-run mode.
4. Compare expected affected row counts with dev.
5. Run the production migration and save logs.
6. Run production verification queries and spot-check public pages.
7. Record rollback instructions and the exact artifact versions used.

Never run destructive production merges without an explicit reviewed merge plan.
