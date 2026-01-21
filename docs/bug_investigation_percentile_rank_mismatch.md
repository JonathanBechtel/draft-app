# Bug Investigation: Percentile vs Rank Mismatch in Player Metrics

## Summary

Investigation into why rank and percentile values don't align mathematically for player metrics.

---

## Key Findings from Database Queries

### Finding 1: `extra_context["population_size"]` is NULL everywhere

Every metric value row has `stored_pop_size: null`. This means the system falls back to dynamic recalculation via `_metric_population_size()`.

### Finding 2: Three-Quarter Sprint (lower_is_better) - TIES explain the discrepancy

For `current_draft` + `wing` cohort:
- rank: 14, percentile: 21.74, population: 23

**Math verification:**
- 13 players strictly faster (< 3.3s) → rank = 14
- 19 players as fast or faster (≤ 3.3s) → percentile = (1 - 19/23) * 100 + 4.35 = 21.74%
- **6 players have exactly 3.3s** (ties)

**This is correct behavior** - rank and percentile handle ties differently:
- Rank: "You're #14 (tied with 5 others)"
- Percentile: "78% are as fast or faster than you"

### Finding 3: Max Vertical (higher_is_better) - REAL BUG CONFIRMED

For `current_nba` + `wing` cohort:
- rank: 15, percentile: 42.73, population: 448

**Math verification:**
- If rank = 15: `positions = 448 - 15 + 1 = 434`
- Expected percentile: `(434/448) * 100 = 96.9%`
- **Actual percentile: 42.73%**

Working backwards from percentile:
- `positions = 0.4273 * 448 = 191`

**The rank implies 434 players ≤ 35.5", but percentile implies only 191 players ≤ 35.5".**

This is a ~243 player discrepancy - impossible if computed from the same baseline.

---

## Root Cause Analysis

### The Bug: Baseline Population Mismatch

Looking at `_annotate_baseline()` in `compute_metrics.py`:

```python
elif self.cohort == CohortType.current_nba:
    baseline = active_series.astype(bool)  # Only is_active_nba=True players
```

For `current_nba` cohorts:
- **Percentile** is computed against only active NBA players (~191 with wing position)
- **Rank** appears to be computed against ALL combine participants (448)
- **Population displayed** is the snapshot total (448)

Cooper Flagg isn't an active NBA player, so there's a mismatch between:
1. The baseline used for percentile (active NBA only)
2. The population used for rank/display (all participants)

### Why `extra_context["population_size"]` is NULL

The code at `compute_metrics.py:1000-1003` should store it:
```python
extra_context={
    "population_size": int(row.population_size)
    if pd.notna(row.population_size)
    else None
}
```

But `row.population_size` may be coming back as NA/null, causing the None storage.

---

## Issues Identified

| Issue | Severity | Description |
|-------|----------|-------------|
| Baseline mismatch for current_nba | **HIGH** | Percentile uses active-only baseline, but rank/population use full cohort |
| NULL population_size | **MEDIUM** | `extra_context["population_size"]` not being stored, forcing dynamic recalculation |
| Ties confusion (UX) | **LOW** | For lower_is_better metrics, ties cause rank and percentile to diverge (expected behavior, but confusing) |

---

## Files Involved

| File | Lines | Issue |
|------|-------|-------|
| `app/cli/compute_metrics.py` | 789-811 | `_annotate_baseline()` - baseline_flag logic differs by cohort |
| `app/cli/compute_metrics.py` | 858-908 | `_compute_metrics()` - percentile/rank computation |
| `app/cli/compute_metrics.py` | 1000-1003 | `extra_context` population_size storage |
| `app/services/metrics_service.py` | 233-318 | `_metric_population_size()` fallback recalculation |

---

## Recommended Fixes

### Fix 1: Add diagnostic logging and recompute metrics (HIGH priority)

The bug is confirmed but the exact cause in the computation code is unclear. The code at `compute_metrics.py:885-908` SHOULD produce consistent results, but the stored data shows otherwise.

**Action items:**
1. Add diagnostic logging to `_compute_metrics()` to capture:
   - `len(sorted_baseline)` vs `baseline_count`
   - Sample `positions` values vs `pos_right` values
   - Verify they're identical
2. Recompute metrics for a single cohort (e.g., `current_nba` + `wing`) with logging enabled
3. Check if newly computed metrics are consistent

### Fix 2: Store population_size in extra_context (MEDIUM priority)

The `extra_context["population_size"]` is NULL everywhere. Fix in `_compute_metrics()`:

```python
# Line ~936: Ensure population_size column uses baseline_count
"population_size": pd.Series(baseline_count, index=df.index, dtype="int64"),
```

Also verify line 1001-1003 is correctly storing this value.

### Fix 3: Audit snapshot population_size (MEDIUM priority)

The snapshot `population_size` (448) differs from the actual baseline used for computation (~110 for active NBA). Consider:
- Store `baseline_count` instead of `total_population` on the snapshot
- Or add a separate `baseline_count` field

### Fix 4: UX improvement for ties (LOW priority)

For lower_is_better metrics, the ties cause rank and percentile to diverge (expected behavior, but confusing). Consider displaying tie information.

---

## Verification Steps

### Step 1: Check if computation code produces consistent results

```python
# Add to _compute_metrics() temporarily
print(f"baseline_count: {baseline_count}")
print(f"len(sorted_baseline): {len(sorted_baseline)}")
print(f"positions[:5]: {positions[:5]}")
print(f"pos_right[:5]: {pos_right[:5] if not spec.lower_is_better else 'N/A'}")
print(f"positions == pos_right: {np.array_equal(positions, pos_right) if not spec.lower_is_better else 'N/A'}")
```

### Step 2: Recompute and verify

```bash
# Recompute metrics for current_nba cohort
python -m app.cli.compute_metrics --cohort current_nba --source combine_agility

# Then verify with SQL
SELECT
    pmv.rank,
    pmv.percentile,
    CASE
        WHEN NOT md.lower_is_better
        THEN ((ms.population_size - pmv.rank + 1)::float / ms.population_size) * 100
    END as expected_pct,
    ABS(pmv.percentile - expected_pct) as diff
FROM player_metric_values pmv
JOIN metric_snapshots ms ON pmv.snapshot_id = ms.id
JOIN metric_definitions md ON pmv.metric_definition_id = md.id
WHERE md.metric_key = 'max_vertical_in'
  AND ms.cohort = 'current_nba'
  AND ms.is_current = true
ORDER BY diff DESC NULLS LAST
LIMIT 20;
```

### Step 3: Compare before/after

If recomputed metrics are consistent, the bug was in a previous computation run. If still inconsistent, the bug is in the current code.
