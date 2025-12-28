# Player Similarity Brainstorm

## Goals and Constraints
- Produce a repeatable similarity score per player between 0 and 100.
- Support per-dimension comps (anthropometrics, combine performance, shooting) plus optional composite scores.
- Leverage existing metric snapshots and their standardized outputs (z-score, percentile) for fast iteration.
- Keep methods flexible to add new metrics or snapshots without bespoke feature engineering.
- Provide tunable weighting for position, era, and scout preferences.

## Source Data
- Metric snapshots computed by `app/cli/compute_metrics.py` (`docs/metrics_pipeline.md`). Each snapshot exposes raw values, z-scores, percentiles, and ranks for every metric in scope.
- Identity scaffolding (`docs/data_model.md`) allows grouping by cohort, season, and position buckets. These factors power cohort-specific scaling and candidate pools.
- Additional derived features can be layered (e.g., wingspan-to-height ratio) prior to similarity calculations when they improve interpretability.

## Dimension-Specific Engines

### Anthropometrics
- **Mahalanobis distance on z-score vectors**
  - *Strengths*: Accounts for correlated measurements (height, reach, wingspan). Robust when metrics share units. Converts distance to similarity via `score = 100 * exp(-α * d)`.
  - *Weaknesses*: Requires well-estimated covariance, so small cohorts need shrinkage or regularization. Sensitive to outliers; consider winsorizing.
  - *Technical notes*: Fit covariance per `(position_bucket, era)` to avoid cross-position bias. Fallback to diagonal covariance when samples are thin.

- **Gower similarity for mixed feature sets**
  - *Strengths*: Handles missing values gracefully and mixes continuous + categorical (e.g., dominant hand if added later).
  - *Weaknesses*: Less expressive for highly correlated continuous metrics; similarity range needs rescaling to 0–100.
  - *Technical notes*: Implement via vectorized pandas/NumPy operations with mask penalties for missing pairs.

### Combine Performance (Agility, Speed, Strength)
- **Standardized Euclidean distance (per-metric variance)**
  - *Strengths*: Easy to interpret and explain; aligns with existing percentiles.
  - *Weaknesses*: Treats metrics independently; ignores covariance between drills (e.g., lane agility vs shuttle).
  - *Technical notes*: Use snapshot-level z-scores. Penalize comparisons when overlap < configurable threshold (e.g., <70% shared drills) by reducing similarity.

- **Dynamic time warping / shape-based comparison (for sequential drills)**
  - *Strengths*: If multi-split timing data becomes available, handles temporal patterns (borrowed from gait analysis).
  - *Weaknesses*: Overkill for scalar drill outputs; fallback idea for future richer datasets.

### Shooting Drills
- **Cosine similarity on efficiency vectors (FGM/FGA, %, volume)**
  - *Strengths*: Measures directional similarity, highlighting shot profile match even when absolute volume differs.
  - *Weaknesses*: Requires vector normalization; zero vectors (no attempts) need handling.
  - *Technical notes*: Construct metrics per drill: percentage, attempts, makes. Apply minimum-attempt thresholds.

- **Hellinger distance on shot distribution**
  - *Strengths*: Focuses on proportional shot mix; common in recommendation systems.
  - *Weaknesses*: Ignores absolute volume/performance if not combined with efficiency features.
  - *Technical notes*: Convert drill attempt counts to probability simplex; transform to similarity with exponential decay.

## Composite Scoring Strategies
- **Weighted additive blend**
  - Combine per-dimension distances: `d_total = w_a * d_anthro + w_c * d_combine + w_s * d_shoot` with `w_a + w_c + w_s = 1`.
  - *Strengths*: Transparent, easy to tune per scouting persona (e.g., defense-heavy vs shooting-heavy).
  - *Weaknesses*: Requires weight calibration; default weights may bias toward data-rich dimensions.
  - *Details*: Offer presets (guards, wings, bigs) and allow user-defined overrides.

- **Learned metric (Mahalanobis / LMNN / Siamese network)**
  - Train on historical outcomes (NBA roles, RAPTOR similarity) to learn a distance metric maximizing meaningful comps.
  - *Strengths*: Captures cross-feature interactions automatically.
  - *Weaknesses*: Needs labeled pairs or outcomes; risk of overfitting given limited samples.
  - *Details*: Start with Mahalanobis metric learning for interpretability; store learned matrix per cohort.

- **Archetype clustering with centroid distance**
  - Cluster players (k-means, self-organizing maps) within each dimension; similarity is inverse distance to shared centroid.
  - *Strengths*: Delivers narrative labels (“Stretch big with elite wingspan”).
  - *Weaknesses*: Relies on stable clusters; may blur unique comps.
  - *Details*: Persist cluster IDs with metadata so UI can surface archetype matches alongside raw neighbors.

## Normalization and Cohort Controls
- Define cohorts by `(position_bucket, season_range)` to localize comparisons (e.g., current draft guards vs historical guards).
- Compute scaling parameters (means, variances, covariance) per cohort; store with snapshot or recompute on demand.
- Allow cross-cohort comps by converting to percentile or rank space before distance calculations.
- Apply overlap penalties or exclusion rules when shared metric coverage falls below thresholds to avoid noisy matches.

## Score Mapping (Distance → 0–100)
- Baseline: `score = 100 * exp(-α * d)` with α tuned so median neighbor sits near 50.
- Alternate: Percentile rank neighbors within cohort and rescale linearly (`score = 100 * (1 - percentile_rank)`).
- Ensure floor/ceiling guards: clamp scores `[0, 100]`, reserve 100 for identical vectors.

## Implementation Outline
1. **Feature assembly**: For a snapshot, pivot metrics into matrices per dimension, applying imputation or masks for missing data.
2. **Cohort prep**: Segment players by position bucket and era window; compute per-cohort statistics needed by the chosen distance metric.
3. **Similarity computation**: For each player, compute distances within cohort, transform to scores, and capture top-N neighbors per dimension.
4. **Composite layer**: Blend per-dimension distances under selected weight scheme; output overall similarity list plus supporting metadata (weights, overlap, z-score deltas).
5. **Persistence**: Store neighbors in a `player_similarity` table keyed by `snapshot_id`, `player_id`, `dimension` (anthro/combine/shoot/composite), `neighbor_id`, `score`, `distance`, and coverage stats.
6. **Validation**: Spot-check results with domain experts; adjust weights, penalties, or normalization windows based on feedback.

## Analytical Considerations
- Monitor metric coverage distribution—some drills are sparsely sampled. Consider imputation strategies (cohort mean, regression) or explicit “missing metric” penalties.
- Track sensitivity of scores to weight changes; provide tooling to visualize how comps shift when scouts adjust sliders.
- Validate against historical player pairs widely considered comps to ensure qualitative alignment.
- Keep detailed logging of distance components so explanations (“+2.1" wingspan vs comp”) are easily surfaced.

## Open Questions
- What minimum sample size per cohort ensures stable covariance estimates? Is shrinkage (e.g., Ledoit-Wolf) necessary?
- Should composite scores include advanced stats (future data sources) once available, and how will that affect weights?
- How often should similarity snapshots be recomputed—per metric run, nightly batch, or on demand?
- What UI/UX cues best communicate when a comp is driven by limited overlap vs comprehensive similarity?
