# Summer League Data Backbone QA Checklist

**Sources:**
- Raw-ingestion spec: `docs/plans/summer-league-raw-ingestion-workflow.md`
- Feature plan: `docs/summer_league_stats_plan.md`
- Operator runbook: `docs/summer_league_backbone_runbook.md`
- Repo orchestration guide: `docs/plans/ai-orchestrator-ticket-spec.md`
- GitHub master issue: `#319`

**Sibling artifact:** test plan at `summer-league-data-backbone-test-plan.md`

**Final QA evidence:** `docs/qa/summer-league-backbone-qa-2026-06-10.md`
passed with `0` errors after PR `#343`.

This checklist defines product-level behaviors QA should verify before considering
the Summer League data backbone complete. The project is a backend data pipeline:
there are no public routes, templates, share cards, or admin UI in scope. The
acceptance bar is durable source preservation, auditable ingestion, normalized
facts, player resolution, idempotency, and high-quality final evidence.

## Raw Archive Integrity

- The completed raw scrape is durably preserved or explicitly accounted for.
  - Verify: run the archive planner against `data/raw/nba_stats/summer_league`.
  - Expected: every discovered local raw file maps to an S3 key under
    `raw/nba_stats/summer_league/{year}/{league_id}/...` with the same relative
    layout.
  - Evidence: archive report with planned/uploaded/skipped/error counts and
    sample source-to-key mappings.

- Dry-run archive mode performs no network writes.
  - Verify: run `scripts/archive_summer_league_raw.py --dry-run`.
  - Expected: reports the intended keys, checksums, and skip decisions without
    uploading or mutating remote objects.
  - Evidence: dry-run report and mocked S3 write assertions in tests.

- Archive reruns do not duplicate unchanged objects.
  - Verify: run the archive command twice with the same fixture or real slice.
  - Expected: unchanged files are skipped by checksum/size where practical;
    changed files require `--force` or are clearly reported.
  - Evidence: second-run report showing stable counts and skips.

## Raw Audit Completeness

- Every raw manifest is represented by one `summer_league_raw_runs` row.
  - Verify: audit a selected `(year, league_id)` slice.
  - Expected: one run row stores year, LeagueID, venue slug, status, manifest
    path, manifest checksum, row counts, game count, error count, and optional
    durable archive key.
  - Evidence: DB row plus audit report summary.

- Every expected raw file is represented by one `summer_league_raw_files` row.
  - Verify: compare manifest-listed games/endpoints to audited file rows.
  - Expected: present, missing, empty, parseable, failed, and skipped files are
    all visible through `parse_status`, `parse_error`, row count, checksum, byte
    size, and relative path fields.
  - Evidence: endpoint coverage report and sample rows for success and failure
    statuses.

- Audit reruns are idempotent.
  - Verify: run the audit scanner twice over the same raw root.
  - Expected: run/file counts remain stable; rows update timestamps and metadata
    without duplicate rows.
  - Evidence: first and second run counts with uniqueness checks.

## Normalization Parity

- One competition row exists per audited `(year, league_id)`.
  - Verify: normalize each stress slice after audit.
  - Expected: `summer_league_competitions` stores the source year, LeagueID,
    venue slug, display name, quality flags, date range, and raw run link.
  - Evidence: normalized competition rows for modern and historical slices.

- Game, team, and team-log counts preserve raw source counts.
  - Verify: compare manifest `game_count`, distinct source team IDs/names, and
    parsed team box rows to normalized tables.
  - Expected: `summer_league_games`, `summer_league_team_entries`, and
    `summer_league_team_game_logs` match the audited source inventory, allowing
    documented partial-data gaps.
  - Evidence: parity table in QA report.

- Source players and player game logs preserve NBA.com identity.
  - Verify: compare parsed player rows and `PERSON_ID` values to
    `summer_league_source_players` and `summer_league_player_game_logs`.
  - Expected: every player game log has a valid `source_player_id`,
    `nba_stats_person_id`, raw player name, team, game, and competition. The
    canonical `player_id` may be null only when the source player is unresolved
    or review-needed.
  - Evidence: row-count parity and spot checks for resolved and unresolved rows.

## Player Resolution

- Existing NBA.com external IDs resolve first.
  - Verify: seed `player_external_ids(system="nba_stats", external_id=...)` and
    run resolution.
  - Expected: the matching source player is linked to the canonical player with
    `EXTERNAL_ID` status and existing game logs receive `player_id`.
  - Evidence: source-player row, external-ID row, and updated game logs.

- Exact name and alias matches resolve safely.
  - Verify: seed players and aliases that match normalized source names.
  - Expected: exact matches use `EXACT`; alias matches use `ALIAS`; confidence
    and resolution metadata are recorded.
  - Evidence: resolution report and DB rows.

- Ambiguous candidates do not auto-resolve.
  - Verify: seed multiple plausible canonical candidates for one source player.
  - Expected: source player remains unresolved or `VECTOR_CANDIDATE`, candidate
    evidence is stored, and a pending review row is created when the review
    queue is implemented.
  - Evidence: candidate JSON and optional review row.

- Stub creation is explicit and complete.
  - Verify: run with `--create-stubs` for a no-candidate source player.
  - Expected: creates a `players_master` stub, writes
    `player_external_ids(system="nba_stats", external_id=PERSON_ID)`, links the
    source player, and backfills `player_id` on logs.
  - Evidence: created stub, external ID, source-player resolution, and log link.

## Historical Edge Cases

- Modern full competitions classify as `full` when all expected endpoints are
  usable.
  - Verify: run final QA on `2024/15` or `2025/15`.
  - Expected: box, advanced, scoring, play-by-play, and shot-chart availability
    are reflected honestly in quality flags and endpoint coverage.
  - Evidence: QA report for the modern Vegas slice.

- Satellite and older competitions classify partial data honestly.
  - Verify: run final QA on `2024/13`, `2010/14`, and `2007/15`.
  - Expected: missing PBP, shot-chart, old endpoint shape, and incomplete raw
    coverage do not block box-score normalization; quality is `partial`,
    `box_only`, or `raw_only` as appropriate.
  - Evidence: per-slice quality classification and endpoint findings.

- Corrupt or malformed raw files fail locally, not globally.
  - Verify: run audit/QA against a fixture containing a corrupt JSON file.
  - Expected: one file records `PARSE_FAILED` with a parse error; the rest of
    the competition can still be audited and normalized where possible.
  - Evidence: negative-case fixture test and QA finding.

## Referential Integrity

- Normalized fact tables contain no orphaned references.
  - Verify: run QA validators after backfill.
  - Expected: no orphaned player logs, team logs, games, team entries,
    competitions, source players, or canonical player links.
  - Evidence: referential-integrity findings are empty or non-blocking.

- Uniqueness constraints protect stable NBA.com IDs.
  - Verify: run schema and negative-case tests.
  - Expected: duplicate `nba_stats_game_id`, duplicate `PERSON_ID`, duplicate
    `(game, source player, team)`, and duplicate `(game, team)` facts are
    rejected or upserted deterministically.
  - Evidence: integration tests and QA duplicate checks.

## Idempotency And Re-run Behavior

- Full backfill is idempotent.
  - Verify: run the end-to-end backfill for a small fixture slice twice.
  - Expected: counts, checksums, source IDs, canonical links, and quality flags
    remain stable on the second run unless `--force` changes parsed rows
    deterministically.
  - Evidence: before/after snapshot in tests and final QA report.

- Dry-run mode reports intended writes without DB mutation.
  - Verify: run audit/backfill commands with `--dry-run` where supported.
  - Expected: reports planned inserts/updates and identifies any stage that
    cannot support a true dry-run.
  - Evidence: command output and DB count comparison.

- Force mode is deterministic.
  - Verify: change a fixture payload or audit metadata, then rerun with
    `--force`.
  - Expected: affected rows update consistently; unrelated rows are unchanged.
  - Evidence: focused force-mode test or QA note.

## Final QA Gate

Final QA is complete when `#333` demonstrates:

1. Required repo checks pass through Conda per `docs/plans/ai-orchestrator-ticket-spec.md`.
2. The Summer League QA harness runs against `2024/15`, `2024/13`, `2010/14`,
   and `2007/15`.
3. Raw archive/audit, normalization parity, player resolution, historical edge
   cases, referential integrity, idempotency, and failure behavior are covered
   in one Markdown report under `docs/qa/`.
4. Blocking findings are fixed or explicitly documented as accepted follow-up
   gaps with concrete evidence.
5. The final report contains enough counts, examples, and failure summaries for
   another agent to diagnose the pipeline without manually reading raw JSON.
