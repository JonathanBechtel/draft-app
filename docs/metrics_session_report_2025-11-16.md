# Metrics Backfill + Snapshot Cleanup — 2025-11-16

## Scope
- Audited and computed metric snapshots across cohorts using combine data.
- Backfilled missing runs (parent and fine/hybrid scopes) where data exists.
- Fixed mislabeled “advanced_stats” snapshots by splitting into per-source combine snapshots.
- Added targeted tooling to make backfills repeatable and less error-prone.

## Key Scripts & Changes
- `app/cli/compute_metrics.py`
  - Added `--sources` to limit computation to specific sources (e.g., `combine_anthro`, `combine_agility`, `combine_shooting`).
  - Fixed early-return bug in snapshot creation flow so snapshots persist when results exist.
- New: `app/cli/backfill_metrics.py`
  - Plans and executes missing runs across cohorts/seasons with optional parent/fine sweeps.
  - Uses unique run keys per anchor (season + scope) to avoid collisions during large backfills.
- New: `app/cli/split_advanced_snapshots.py`
  - Splits mislabeled snapshots (`source=advanced_stats`) into real per-source snapshots by inspecting `PlayerMetricValue → MetricDefinition.source`.

## Baseline Definitions (as coded)
- `current_draft` / `all_time_draft`: everyone in the (filtered) cohort is baseline.
- `current_nba`: baseline = `is_active_nba=True`.
- `all_time_nba`: baseline = `is_active_nba=True` OR `nba_last_season` is set.
- All-time cohorts dedupe per player to the most recent combine season before computing.

## Position Scopes
- Parent: `guard`, `wing`, `forward`, `big`.
- Fine/Hybrid: `pg`, `sg`, `sf`, `pf`, `c`, `pg-sg`, `sg-sf`, `sf-pf`, `pf-c`.
- Internally, fine tokens are normalized with underscores (e.g., `sg_sf`).

## What We Ran (highlights)
- All-Time Draft (parent + baseline; fine/hybrids) — anthro + agility only
  - Parent + baseline (created): run-key prefix `metrics_all_20251116043259`.
  - Fine/hybrids (created): run-key prefix `metrics_all_20251116043811__{fine}`.
- Current NBA (fine/hybrids, baseline skipped) — anthro + agility
  - Created: run-key prefix `metrics_all_20251116044747__{fine}`.
- All-Time NBA (fine/hybrids, baseline skipped) — anthro + agility
  - Created: run-key prefix `metrics_all_20251116045238__{fine}`.
- Current Draft (2000-01) — parent + fine/hybrids, baseline skipped — anthro + agility
  - Parent: `metrics_2000-01_20251116052009__{parent}:{source}`.
  - Fine: `metrics_2000-01_20251116052348__{fine}:{source}`.
- Current Draft (all seasons 2000-01 → 2025-26) — parent + fine/hybrids, baseline skipped — anthro + agility
  - Backfilled with `backfill_metrics.py` (unique run keys per scope to avoid collisions).
- Current Draft (2025-26) — baseline (all positions)
  - `metrics_2025-26_20251116053453:{source}` persisted for available sources.

Notes:
- Shooting was excluded in most matrix runs due to sparse fine/hybrid baselines; can be backfilled later with a lower `--min-sample`.

## Snapshot Cleanup (advanced_stats bug)
- All-Time Draft: split the merged `advanced_stats` snapshot into three: `combine_anthro`, `combine_agility`, `combine_shooting`.
- Current Draft: split each `advanced_stats` per-season baseline into per-source snapshots with run-keys like `{old_run_key}:{source}`. The original snapshot IDs were deleted after reassignment.

## Data Sanity Checks (selected)
- All-Time Draft (parent minimum non-null counts per source, deduped to latest season):
  - Anthro: guard 454, wing 248, forward 469, big 354.
  - Agility: guard 703, wing 448, forward 842, big 641.
  - Shooting: sparse for some drills/parents (e.g., `midrange_star` often near zero).
- Current NBA (fine/hybrids; active-only baseline; deduped):
  - Anthro min by token: `pg 64`, `sg 77`, `sf 45`, `pf 67`, `c 32`, hybrids smaller.
  - Agility min by token: `pg 32`, `sg 26`, `sf 20`, `pf 24`, `c 12`, hybrids smaller.
  - Shooting: very sparse at fine/hybrid granularity.
- All-Time NBA (fine/hybrids; active OR history baseline; deduped):
  - Anthro min by token: `pg 125`, `sg 159`, `sf 99`, `pf 148`, `c 59`.
  - Agility min by token: `pg 81`, `sg 94`, `sf 68`, `pf 93`, `c 39`.
- Current Draft per-season (anthro/agility):
  - Parent scopes are consistently viable (several metrics meet `min-sample >= 3`).
  - Fine/hybrids viable for core positions; hybrids vary by season (expected).
  - Identical “metrics meeting threshold” counts across parents in some seasons are expected when certain columns are universally null (e.g., `height_w_shoes_in`, `body_fat_pct`, `bench_press_reps`).

## How To Reproduce
- Make (one season, parent sweep, skip baseline):
  - `make metrics SEASON=2000-01 POSITION_MATRIX=parent MATRIX_SKIP_BASELINE=1 CATEGORIES="anthropometrics combine_performance"`
- Make (one season, fine/hybrid sweep, skip baseline):
  - `make metrics SEASON=2000-01 POSITION_MATRIX=fine MATRIX_SKIP_BASELINE=1 CATEGORIES="anthropometrics combine_performance"`
- Limit sources:
  - `METRIC_ARGS="--sources combine_anthro combine_agility"`
- Backfill planner/executor (all seasons, current_draft, parent+fine, skip baseline):
  - `python -m app.cli.backfill_metrics --cohorts current_draft --sources combine_anthro combine_agility --no-baseline --include-fine --execute --verbose`
- Split mislabeled snapshots:
  - `python -m app.cli.split_advanced_snapshots --cohorts current_draft --execute`

## Recommendations & Next Steps
- Shooting: backfill with a lower `--min-sample` for fine/hybrids if you want broader coverage.
- Position tagging fallback: optionally derive `position_fine/parents` from `raw_position` during compute to handle any rows missing normalized tags.
- Housekeeping:
  - Consider adding `ON DELETE CASCADE` for snapshot FKs (`player_metric_values`, `player_similarity`) in a migration to simplify cleanup.
  - Export a CSV summary of current snapshots (cohort, season, scope, source, population) as a quick audit artifact.

## References
- Compute: `app/cli/compute_metrics.py`
- Backfill: `app/cli/backfill_metrics.py`
- Snapshot split: `app/cli/split_advanced_snapshots.py`
- Make target: `Makefile` (`metrics`)
