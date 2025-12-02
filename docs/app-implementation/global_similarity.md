Global Similarity + Global Cohort
=================================

Context
-------
- Added a `global_scope` cohort to `CohortType` (alembic migration `7b3f2b3c7a4b_add_global_cohort.py`; merge head `8c9a3d9c1f4e_merge_heads_global_cohort.py`).
- Head-to-head similarity selection now prefers `cohort=global_scope` snapshots for the relevant source/dimension; falls back to other current snapshots if none exist.
- Similarity tooling accepts a `--cohort` filter so you can target global snapshots.

How to generate global (all-seasons) snapshots
----------------------------------------------
1) Run metrics with global scope (all seasons):
   ```
   python -m app.scripts.compute_metrics --cohort global_scope --season all --sources combine_anthro combine_agility combine_shooting
   ```
   - Run keys will look like `metrics_global_all|pos=all|min=3_<source>`.
   - These snapshots are created with `is_current=False` (original behavior retained).

2) Mark the desired global snapshots current (required for similarity selection), e.g.:
   ```sql
   UPDATE metric_snapshots
   SET is_current = TRUE
   WHERE cohort = 'global_scope' AND run_key LIKE 'metrics_global_all%';
   ```
   (unset older ones as needed if you only want one current per source).

3) Compute similarity against global snapshots:
   ```
   python -m app.scripts.backfill_similarity --cohort global_scope --execute
   ```
   - Use `--max-neighbors 10` to cap stored neighbors per player/dimension.

Operational notes
-----------------
- `backfill_similarity` processes one snapshot at a time: fetch z-scores for that snapshot → compute similarity → insert rows → commit, then move to the next snapshot.
- If you need to regenerate similarity, it is safe to clear `player_similarity` (e.g., `DELETE FROM player_similarity;`) and re-run the backfill.
- The head-to-head API will pick up global similarity once a `cohort=global_scope` snapshot is marked current for the relevant source.

Open choices
------------
- If you prefer snapshots to be created as current during the metrics run, flip `is_current` to `True` in `compute_metrics` for the global run (was reverted to keep original behavior). For now, manual update or a follow-up run is required.***
