# Metric Snapshots: Versioning, Selection, and Run Key

This document captures the agreed design for metric snapshots: a human‑readable `run_key`, per‑group versioning, a single current marker, and safe deletes with cascades.

## Goals
- Make it obvious which snapshot powers the app for a given configuration.
- Allow legitimate re‑runs with identical args (new data may yield new results).
- Keep run keys human‑readable and deterministic (no timestamps).
- Enable clean deletion of erroneous runs with `ON DELETE CASCADE`.

## Grouping & Identity
- Versioning group: `(cohort, source, run_key)`
  - `source` is `MetricSource` (e.g., `combine_anthro`, `combine_agility`, `combine_shooting`).
  - `run_key` encodes arguments for the run (see below); cohort is captured in its own column.
- A snapshot contains many metric definitions for a single `source`.
- Current-selection context (what the app/UI selects against): `(cohort, source, season_id, position scope)`.

## Run Key Format (Human‑Readable)
- Canonical format (fixed segment order, no timestamps):
  - `cohort=<cohort>|season=<code-or-all>|pos=<token>|min=<n>`
- Rules:
  - Always include `pos=`; use `pos=all` when no position scope is specified (baseline).
  - Use current position tokens (e.g., `pg`, `wing`, `pg-sg`, …) as emitted by the script.
  - Always include `min=<n>` (minimum sample) because it materially changes statistics.
  - Keep identifiers lowercase; preserve season code formatting (e.g., `2024-25`).
- Examples:
  - `cohort=current_draft|season=2024-25|pos=all|min=3`
  - `cohort=current_draft|season=2024-25|pos=wing|min=3`
  - `cohort=all_time_nba|season=all|pos=pg-sg|min=3`
- Matrix baseline flag (`--matrix-skip-baseline`) affects which snapshots are generated (baseline present or not), not the run key shape.

## Snapshot Columns (additions)
- `version INT NOT NULL` — monotonically increasing within `(cohort, source, run_key)`.
- `is_current BOOLEAN NOT NULL DEFAULT false` — marks the single snapshot in use within a context (cohort + season + position scope).

## Constraints (Postgres)
- Exactly one current per context:
  - Partial unique index on `(cohort, source, season_id, position_scope_parent, position_scope_fine)` where `is_current = true`.
- Version uniqueness:
  - Unique constraint on `(cohort, source, run_key, version)`.
- No uniqueness on `run_key` alone. Identical args across time are allowed and become higher `version` values within the same group.
- No extra supporting indexes are required (table is small).

## Selection Semantics
- Canonical app query: select the current snapshot for a configuration (cohort + optional season + position scope)
  - Filter to the desired context and select `is_current = true`.
- Optional fallback: if there is no current, select the newest snapshot (e.g., `ORDER BY calculated_at DESC LIMIT 1`).

## Versioning & Promotion
- Version is scoped to `(cohort, source, run_key)`.
- Auto‑increment policy (choose at implementation time):
  - App‑managed: transactionally `SELECT max(version)` for the group and insert `max+1` (backed by the unique constraint).
  - DB‑managed: a small `BEFORE INSERT` trigger that sets `NEW.version = (SELECT coalesce(max(version),0)+1 FROM metric_snapshots WHERE source=NEW.source AND run_key=NEW.run_key)`.
- Promotion to current (atomic):
  - Single statement (context-scoped):
    ```sql
    UPDATE metric_snapshots
    SET is_current = CASE WHEN id = :target_id THEN true ELSE false END
    WHERE cohort = :cohort
      AND source = :source
      AND season_id IS NOT DISTINCT FROM :season_id
      AND position_scope_parent IS NOT DISTINCT FROM :position_scope_parent
      AND position_scope_fine IS NOT DISTINCT FROM :position_scope_fine;
    ```
  - Or two statements inside a transaction: demote current → promote target.
- Current does not have to be the highest version (enables rollbacks).

## Delete Semantics & Safety
- Child/result tables reference snapshots with `ON DELETE CASCADE` so erroneous runs can be removed cleanly.
- `ON DELETE CASCADE` is unconditional — it does not check `is_current`.
- To enforce “delete only when non‑current,” use a DB guard:
  - Recommended trigger:
    ```sql
    CREATE OR REPLACE FUNCTION prevent_delete_current_snapshot()
    RETURNS trigger AS $$
    BEGIN
      IF OLD.is_current THEN
        RAISE EXCEPTION 'Cannot delete current snapshot (id=%). Demote first.', OLD.id;
      END IF;
      RETURN OLD;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER trg_prevent_delete_current
    BEFORE DELETE ON metric_snapshots
    FOR EACH ROW EXECUTE FUNCTION prevent_delete_current_snapshot();
    ```
  - Alternatively, enforce at the app/service layer (lighter, less safe).

## Child Tables (Results)
- Foreign keys to `metric_snapshots(id)` should use `ON DELETE CASCADE`.
- Guard against intra‑snapshot duplicates with unique keys that include `snapshot_id`.
  - Already present for `player_metric_values`: unique `(snapshot_id, metric_definition_id, player_id)`.

## Script Integration (no code in this doc)
- File: `app/scripts/compute_metrics.py`
- Replace the timestamped default run key with the canonical format above and always emit `pos=` (use `pos=all` when no scope).
- Compose a single shared `run_key` across sources; do not suffix the key with the `source` (the snapshot’s `source` column provides that).
- If performing “replace run,” target exact `(source, run_key)` rather than `LIKE` patterns.

## Migration & Rollout
- Fresh start (preferred, since data is disposable):
  - Drop existing snapshot data.
  - Alter schema to add `version` and `is_current` columns.
  - Add unique `(source, run_key, version)` and partial unique on `(source, run_key)` where `is_current`.
  - Ensure child FKs use `ON DELETE CASCADE`.
  - Optionally add the delete‑guard trigger.
- Backfill path (if ever needed):
  - Recompute deterministic `run_key` values (no timestamps, include `pos` and `min`).
  - Populate `version` within each `(source, run_key)` ordered by `calculated_at` (tie‑break by `id`).
  - Set `is_current=true` for the intended current (typically highest version).

## Manual Toggling Behavior
- Setting `is_current=false` on the current row without promoting another leaves the group with no current; selection queries that require `is_current=true` will return nothing.
- Promoting any version to current automatically demotes others in the group via the atomic update shown above; the partial unique index guarantees a single current.

## Rationale Summary
- Human‑readable `run_key` makes intent obvious (cohort/season/pos/min).
- Versioning per `(source, run_key)` captures legitimate re‑runs without conflicts.
- `is_current` + partial unique index provides an unambiguous selection for the app.
- `ON DELETE CASCADE` enables clean removal; trigger prevents accidental deletion of the live snapshot.
