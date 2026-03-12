# Physical Similarity Specification

## Goal

Define a player-to-player score that answers a narrow question:

> "How physically similar are these two individuals?"

This score should reflect body template and frame, not overall basketball profile.

It must be:

- intuitive to users,
- stable across cohorts and time,
- explainable from raw measurements,
- independent of who else is in the dataset.

## Summary

Physical similarity should be an absolute, measurement-based score computed from raw anthropometric values.

It should not use:

- cohort-relative z-scores,
- nearest-neighbor rank within a snapshot,
- exponential scaling based on the cohort median distance,
- shooting or agility metrics.

Those approaches are appropriate for profile similarity or comps, but not for the user-facing question of physical resemblance.

## Product Recommendation

DraftGuru should expose two distinct concepts:

1. `Physical Similarity`
   - Based only on raw body measurements.
   - Answers: "How similar are these players physically?"

2. `Profile Similarity`
   - Existing or future nearest-neighbor comp system using anthropometrics, combine, shooting, or production.
   - Answers: "How similar are these players as prospects?"

These labels should never be merged into one headline percentage.

## Included Metrics

The physical similarity score should use only anthropometric measurements that describe body dimensions and frame.

Recommended inputs:

- `height_wo_shoes_in`
- `wingspan_in`
- `standing_reach_in`
- `weight_lb`
- `hand_length_in`
- `hand_width_in`

Optional input:

- `body_fat_pct`

Notes:

- `height_w_shoes_in` should not be used if `height_wo_shoes_in` is available.
- If both are present in source data, prefer the without-shoes measurement for stability.
- `body_fat_pct` should be optional because it is often missing and is noisier than the core body-dimension fields.

## Excluded Metrics

The following should not be part of physical similarity:

- lane agility
- shuttle run
- sprint times
- vertical jump
- bench press
- shooting drills
- percentiles
- ranks
- z-scores

These are performance or athleticism measures, not body-shape measures.

## Core Scoring Model

For each included metric:

1. Compute the absolute difference between Player A and Player B.
2. Convert that difference into a per-metric similarity on a 0-1 scale.
3. Take a weighted average across all available metrics.
4. Convert the final value to a 0-100 percentage.

### Per-Metric Difference

For metric `m`:

`diff_m = abs(value_a_m - value_b_m)`

### Per-Metric Similarity Function

Use a smooth Gaussian-style decay:

`sim_m = exp(- (diff_m / tol_m)^2 )`

Where:

- `sim_m` is between 0 and 1,
- `tol_m` is the tolerance for that measurement.

Properties:

- identical measurement -> `1.0`
- very small difference -> remains close to `1.0`
- larger differences decay smoothly
- no hard cliffs

This is preferable to a linear cutoff because it avoids sharp jumps that feel arbitrary.

## Recommended Tolerances

These tolerances should represent the rough range within which users would still view two players as physically close.

Recommended defaults:

| Metric | Tolerance |
|---|---:|
| Height without shoes | `2.0 in` |
| Wingspan | `3.0 in` |
| Standing reach | `2.0 in` |
| Weight | `15.0 lb` |
| Hand length | `0.5 in` |
| Hand width | `0.5 in` |
| Body fat percentage | `2.0 pct` |

Interpretation:

- A 1-inch height gap should still score as very similar.
- A 4-inch wingspan gap should materially reduce similarity.
- A 2-4 pound weight gap should barely matter.
- Small hand-size differences matter because the measurement range is narrow.

These values should be treated as product-tuning defaults, not immutable constants.

## Weights

Body-template metrics should carry most of the score.

Recommended default weights:

| Metric | Weight |
|---|---:|
| Height without shoes | `0.22` |
| Wingspan | `0.26` |
| Standing reach | `0.22` |
| Weight | `0.20` |
| Hand length | `0.05` |
| Hand width | `0.05` |

If `body_fat_pct` is included, start with:

- `body_fat_pct = 0.05`

and proportionally reduce the others.

Rationale:

- wingspan, height, and standing reach define the visible body archetype,
- weight captures frame and build,
- hand dimensions are useful tiebreakers but should not dominate the score.

## Missing Data Rules

The score must be robust to partially missing measurements.

Rules:

1. Compute per-metric similarity only for metrics present for both players.
2. Renormalize weights over the metrics that are available.
3. Require at least 4 core metrics to produce a score.

Core metrics:

- `height_wo_shoes_in`
- `wingspan_in`
- `standing_reach_in`
- `weight_lb`

If fewer than 4 core metrics are shared:

- do not return a headline physical similarity percentage,
- return `insufficient_physical_data`.

This is better than producing a misleading score from only hand measurements or one size field.

## Final Score Formula

For the set of shared metrics `S`:

`physical_similarity = 100 * (sum(weight_m * sim_m for m in S) / sum(weight_m for m in S))`

Clamp to:

- minimum `0`
- maximum `100`

Round for display:

- integer percentage in UI,
- keep one decimal internally if needed.

## Confidence Signal

In addition to the score, compute a confidence label based on coverage.

Recommended rules:

- `High`: all 4 core metrics present, plus at least 1 optional metric
- `Medium`: all 4 core metrics present
- `Low`: score computed with renormalization but missing 1 or more core metrics
- `Unavailable`: fewer than 4 core metrics shared

The UI should show this alongside the score or in a tooltip.

## Explainability Output

Every stored or computed result should include a metric-by-metric breakdown.

Recommended structure:

- metric name
- player A raw value
- player B raw value
- absolute difference
- tolerance
- per-metric similarity
- weight used

This enables UI copy such as:

- "Identical height and wingspan"
- "Weight differs by 4.0 lbs"
- "Hand width is the largest physical difference"

## Example Behavior

The metric should align with user intuition in cases like these:

### Near-identical twins

If two players are:

- same height,
- same wingspan,
- within 0.5 inches of standing reach,
- within 4 pounds of weight,
- within 0.25-0.75 inches on hand measurements,

the physical similarity should usually land in the high 80s to high 90s.

### Same height, different frame

If two players have:

- same height,
- materially different wingspan,
- materially different standing reach,
- 20+ pound weight gap,

the score should fall substantially even if one or two measurements match.

### Similar build, different size tier

If two players differ by:

- 3 inches of height,
- 4 inches of wingspan,
- 3 inches of standing reach,

the score should not remain "very high" even if their weights are close.

## Storage Recommendation

If persisted, this should live as a separate physical-similarity dataset rather than being folded into the current snapshot-relative similarity table semantics.

Recommended fields:

- `player_a_id`
- `player_b_id`
- `physical_similarity_score`
- `confidence_label`
- `shared_metric_count`
- `details` JSON
- `calculated_at`
- `feature_version`

Important:

- This score does not depend on cohort or snapshot context.
- It should be symmetric by definition:
  - `similarity(a, b) == similarity(b, a)`

If stored in pair form, canonicalize ordering so each pair is written once.

## UI Guidance

The UI should label this score explicitly as:

- `Physical Similarity`

It should not be labeled as:

- `Similarity`
- `Comp Score`
- `Profile Match`

Suggested UI treatments:

- headline badge: `92% Physical Similarity`
- tooltip: `Based on height, wingspan, reach, weight, and hand measurements`
- optional secondary text: `High confidence`

## Relationship to Existing Similarity System

The current similarity system is useful for neighbor retrieval and comps because it:

- operates on snapshot-scoped feature vectors,
- uses z-scores,
- ranks neighbors relative to a cohort,
- supports multi-dimensional comp discovery.

That system should remain a separate product surface, likely under:

- `Prospect comps`
- `Profile similarity`
- `Nearest neighbors`

It should not be repurposed as the physical-similarity headline score.

## Calibration and Validation

Before launch, validate the metric with a small set of sanity-check pairs:

1. Identical or near-identical body types should score very high.
2. Same height but clearly different length/frame should score moderately, not extremely high.
3. Distinct archetypes should score low.
4. Missing-data cases should suppress the score rather than fabricate precision.

Recommended review set:

- twins / near-twins,
- classic undersized guard vs larger guard,
- wing vs combo forward with similar height but different reach,
- false-friend cases where percentiles were previously misleading.

## Versioning

The scoring recipe should be versioned explicitly.

Suggested version key:

- `physical_similarity_v1`

Any change to:

- metric set,
- tolerances,
- weights,
- missing-data rules,

should produce a new version.
