# Metrics Snapshot Workflow

This note covers how to generate the derived metrics that populate
`metric_snapshots`, `player_metric_values`, and the downstream UI views.

## Prerequisites

- `DATABASE_URL` in `.env` must point at the database you want to update.
- Run migrations so the metric tables exist: `alembic upgrade head`.
- The combine source tables should already be populated via the scraper +
  ingest workflow described in `docs/scraper.md`.

## Script Entrypoint

Metrics are calculated with the pandas-based CLI at
`app/scripts/compute_metrics.py`.  The script:

- reads combine data into pandas for the requested cohort (current draft,
  all-time draft, etc.),
- computes raw measurement, z-score, percentile, and rank for each metric,
  and
- writes a `MetricSnapshot` plus `PlayerMetricValue` rows (unless
  `--dry-run` is supplied).

Baseline populations are cohort-aware:

- `current_draft` / `all_time_draft`: compare every combine player to the full
  combine population (subject to `--position-scope`).
- `current_nba`: compare against players whose `player_status.is_active_nba` is
  true.
- `all_time_nba`: compare against anyone with NBA history (`is_active_nba` true
  or `player_status.nba_last_season` populated).

Every player with a measurement still receives outputs; the cohort only changes
which rows define the percentile/rank/z-score distribution.

The CLI accepts the arguments shown below (`python -m app.scripts.compute_metrics --help`).

```
--cohort <cohort>            required, one of current_draft | all_time_draft | current_nba | all_time_nba
--season <code>              required when --cohort current_draft (e.g., 2024-25)
--position-scope <pos>       optional position filter (fine: pg, sg, sf, pf, c, pg-sg...
                             parent: guard, wing, forward, big)
--position-matrix <preset>   sweep a preset of scopes (parent = guard/wing/forward/big; fine = pg/sg/sf/pf/c + hybrids)
--matrix-skip-baseline       skip the all-position baseline when using --position-matrix
--categories <...>           optional list of metric categories (anthropometrics, combine_performance, advanced_stats)
--run-key <text>             optional unique identifier (auto-generated timestamped key when omitted)
--min-sample <int>           minimum cohort sample size to emit a metric (default 3)
--notes <text>               optional free-form notes stored with the snapshot
--dry-run                    compute metrics but skip database writes
--replace-run                delete an existing snapshot with the same run key before inserting
```

### Example Commands

Dry-run the 2024–25 draft class metrics for all positions:

```
python -m app.scripts.compute_metrics \
  --cohort current_draft \
  --season 2024-25 \
  --run-key 2024_pre_draft_v1 \
  --dry-run
```

Persist the same snapshot (rerunning after inspection) and restrict to
guards only:

```
python -m app.scripts.compute_metrics \
  --cohort current_draft \
  --season 2024-25 \
  --position-scope guard \
  --run-key 2024_pre_draft_v1_g \
  --replace-run
```

When there is no existing run key, the script generates a timestamped one
(`metrics_<season>_<UTC timestamp>`).

To avoid running multiple commands for positional slices, supply
`--position-matrix parent` (baseline + guard/wing/forward/big) or
`--position-matrix fine` (pg/sg/sf/pf/c plus their common hybrids). Add
`--matrix-skip-baseline` if you have already computed the all-position snapshot
and only need the scoped runs.

## Makefile Target

For convenience, use the `metrics` target added to the root `Makefile`.

```
# dry-run example
make metrics COHORT=current_draft SEASON=2024-25 RUN_KEY=2024_pre_draft_v1 DRY=1

# persist with replace
make metrics COHORT=current_draft SEASON=2024-25 RUN_KEY=2024_pre_draft_v1 REPLACE=1 POSITION=guard
```

Environment variables recognized by the target:

- `COHORT` (required) – cohort slug, defaults to `current_draft`.
- `SEASON` – season code; required when `COHORT=current_draft`.
- `POSITION` – optional position scope (fine tokens like `pg`, `sg`, `sf`, `pf`,
  `c`, or parent buckets `guard`, `wing`, `forward`, `big`).
- `POSITION_MATRIX` – pass `parent` or `fine` to run every scope in that preset
  from a single command.
- `MATRIX_SKIP_BASELINE` – set to `1` to skip the all-position baseline when
  using `POSITION_MATRIX`.
- `RUN_KEY` – optional run identifier.
- `CATEGORIES` – space-separated list of categories passed to
  `--categories` (e.g., `"anthropometrics combine_performance"`).
- `MIN_SAMPLE` – overrides `--min-sample`.
- `NOTES` – note text for the snapshot.
- `DRY` – set to `1` to append `--dry-run`.
- `REPLACE` – set to `1` to append `--replace-run`.
- `METRIC_ARGS` – free-form extra arguments appended to the command.

Example with custom categories and minimum sample:

```
make metrics COHORT=all_time_draft CATEGORIES="anthropometrics" MIN_SAMPLE=10 RUN_KEY=all_time_anthro_v1
```

## Output

Each run prints a summary: population size, snapshot ID (if persisted),
and per-metric diagnostics (count, mean, std or skip reason). On a
non-dry run a new row appears in `metric_snapshots` with the provided run
key, and the corresponding `player_metric_values` are inserted under the
same snapshot ID.

If a metric has fewer than the requested minimum samples it is skipped
for that cohort; re-run the script after filling in more combine data or
lower the `--min-sample` threshold.

## Resetting a Run

To recompute a snapshot, re-run the command with `--replace-run` (or set
`REPLACE=1` with the Makefile target). This deletes the previous
`metric_snapshots` row and all attached `player_metric_values` before the
new values are inserted.

## Future Extensions

- Similarity / nearest-neighbour outputs (`player_similarity`) will reuse
  the same snapshot infrastructure once the feature design is finalized.
- Additional data sources (e.g., NBA tracking data) can plug into the same
  script by registering new `MetricSpec`s.
