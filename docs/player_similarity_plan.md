# Player Similarity Plan

## Scope and Inputs
- Compute similarity per `metric_snapshot` (one run per snapshot) and per dimension: `anthro`, `combine`, `shooting`, plus optional `composite`.
- Inputs: `metric_snapshots` + `player_metric_values` (use z-scores as the default feature space), and snapshot scope fields (`cohort`, `season_id`, `position_scope_parent|fine`).
- Serve “current” results by selecting similarity rows whose `snapshot_id` belongs to `metric_snapshots.is_current = true`; keep historical rows for prior snapshots.

## Feature Assembly
- Build feature frames by pivoting `player_metric_values` for a chosen snapshot:
  - Anthropometrics: wingspan, height (w/wo shoes), reach, weight, body fat, hand length/width.
  - Combine performance: lane agility, shuttle, 3/4 sprint, verticals, bench.
  - Shooting: per-drill FG% (from metrics; attempts used as mask/weights).
- Apply masks for missing metrics; require a minimum overlap fraction (e.g., ≥70%) before scoring a pair.

## Distance and Similarity
- Distance → similarity: `sim = 100 * exp(-α * d)`, clamp to `[0, 100]`. Set α per dimension so the median neighbor distance maps to ~50 (e.g., `α = ln(2) / median_d`, with caps).
- Anthropometrics: Mahalanobis on z-score vectors within the snapshot pool; shrink/fallback to diagonal if covariance is unstable or `n` is small.
- Combine: Standardized Euclidean on z-scores (diagonal covariance).
- Shooting: Cosine distance on the FG% drill vector, masked to drills with attempts for both players; optionally weight drills by min(attempts_i, attempts_j). Skip drills when attempts are below a threshold (e.g., <5).
- Composite: Weighted blend of per-dimension distances (`d_total = w_a*d_a + w_c*d_c + w_s*d_s`); renormalize weights over present dimensions; apply the same exponential mapping.

## Coverage and Confidence
- Track `overlap_pct = shared_metrics / available_metrics` per dimension.
- If `overlap_pct` drops below the threshold, either skip or inflate distance (e.g., `d *= 1 + penalty*(threshold - overlap_pct)`).
- Store per-metric deltas for explainability where useful (especially for anthro).

## Persistence (new table)
- Table keyed by `snapshot_id` (FK to `metric_snapshots`, ON DELETE CASCADE), `player_id`, `dimension`, `neighbor_id`.
- Columns: `distance`, `similarity`, `overlap_pct`, optional `details` JSON (metric deltas, component distances).
- Uniqueness: `(snapshot_id, player_id, dimension, neighbor_id)`.

## Execution Flow
1) Select a snapshot (or all snapshots marked `is_current`) and build per-dimension feature matrices.
2) Compute distances and similarities per dimension, applying coverage rules and α calibration.
3) Insert rows into `player_similarity`; replace existing rows for that snapshot/dimension on rerun.
4) For composite, blend per-dimension distances and persist alongside the components.

## Tooling
- `python -m app.cli.compute_similarity --snapshot-id <id> [--min-overlap 0.7 --weights 0.4 0.35 0.25 --max-neighbors N]` computes similarity for a single snapshot (or `--run-key <key> --source <source>` to resolve the latest version).
- `python -m app.cli.backfill_similarity [--sources combine_anthro combine_agility combine_shooting] [--min-overlap ...] [--weights ...] [--execute]` runs similarity for all `is_current` snapshots (or explicit `--snapshot-ids`); omit `--execute` for a dry-run preview.
- Both scripts use z-scores from `player_metric_values` and write to `player_similarity`.

## Open Decisions
- Exact overlap threshold and penalty curve.
- Default weights for composite (start with anthro 0.4, combine 0.35, shooting 0.25) and whether to expose presets per position bucket.
- Whether to store per-metric deltas in `details` or compute on demand for UI explanations.
