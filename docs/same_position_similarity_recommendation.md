# Recommendation: Fix "Same Position" as a Filter (Position-Parent Overlap)

This document records a recommended fix for the “Same Position Only” checkbox in Player Comparisons.

It is intended as a corrective companion to `docs/same_position_similarity_plan.md`, which proposes computing a second, position-restricted similarity dataset. That approach is not aligned with the current similarity pipeline and introduces multiple correctness and operational risks (detailed below).

## What “Same Position Only” Should Mean

For this app, “Same Position Only” should remain a **filter over already-computed similarity rows**, not a separate similarity computation.

Given the product goal (avoid tiny cohort sample sizes), the filter should be based on **position parent-group overlap**:

- For a given anchor player and candidate neighbor player:
  - Load each player’s `Position.parents` array (e.g., `["guard"]`, `["wing"]`, `["forward"]`, `["big"]`, or multiple values).
  - Treat the pair as a “match” if the intersection is non-empty.
- If either player is missing a `position_id` or `parents`, treat as non-match.

This matches the intent in `docs/same_position_similarity_plan.md` (“guard, wing, forward, big”) while preserving the filter-only semantics used by the current UI/API.

## Current Behavior and Root Cause

The API currently implements `same_position=true` by filtering on `player_similarity.shared_position` in `app/services/similarity_service.py`.

However, the similarity computation pipeline in `app/cli/compute_similarity.py` **does not populate** `PlayerSimilarity.shared_position` when writing similarity rows.

Result: position-filtered queries return zero rows even when a plausible “same position” match exists.

This is a **data annotation gap**, not an absence of computed similarity.

## Why `docs/same_position_similarity_plan.md` Is Misaligned / Risky

`docs/same_position_similarity_plan.md` proposes adding `same_position_only` to `player_similarity` and computing separate similarity rows restricted to a position group.

That design has several issues relative to current code and schema:

1) No `calculate_similarity.py` in this repo
   - The plan asks to “pay particular attention to calculate_similarity.py” / references it as the locus of change, but there is no such file.
   - The actual implementation is in `app/cli/compute_similarity.py`.

2) Uniqueness constraint conflicts with “dual datasets”
   - `player_similarity` is uniquely constrained on `(snapshot_id, anchor_player_id, comparison_player_id, dimension)`.
   - Writing a second set of rows (“same-position-only” and “global”) for the same pairs would violate uniqueness unless the schema is redesigned (not captured in the plan).

3) Current write path deletes per snapshot, so the plan would overwrite itself
   - `app/cli/compute_similarity.write_similarity()` deletes all `player_similarity` rows for a snapshot before inserting new ones.
   - The plan’s loop (“for each position group, call write_similarity”) would repeatedly delete previously inserted rows, leaving only the last processed group.
   - Even if run as separate commands, a later run would wipe the earlier dataset for the same snapshot.

4) Composite similarity becomes inconsistent if computed per group/per dimension
   - `write_similarity()` derives composite similarity from the set of dimensions passed to it.
   - The plan’s pseudocode passes one dimension at a time (`{dim: group_frame}`), which collapses “composite” into a single dimension and breaks consistency with the global composite calculation.

5) Ambiguous grouping and potential duplication
   - A `Position.parents` array can contain multiple parent groups.
   - Splitting frames by parent group can place the same player into multiple groups; without persisting the group identity, a “same_position_only” flag cannot disambiguate which pool produced a row.

For these reasons, the “compute a second dataset” approach should be avoided for this feature.

## Recommended Implementation Strategy (Filter Matches)

Keep the existing API shape and frontend behavior:

- UI continues calling `GET /api/players/{slug}/similar?same_position=true`.
- Service applies “same position” as a filter.

Then fix the filter to use parent overlap.

### Preferred: Compute at Query Time (Avoid Stale Data)

Compute “same position” at read time in `app/services/similarity_service.py`, rather than storing `shared_position` in `player_similarity`.

Rationale:
- Player position assignments can change over time; a stored boolean can become stale.
- The similarity endpoint returns a small number of rows (default 10), so extra joins are typically acceptable.
- This avoids schema migrations and backfills.

Suggested semantics:
- Resolve the anchor player’s `Position.parents` once.
- For candidate neighbor rows, load neighbor `Position.parents`.
- Filter to intersection non-empty.

If the DB query becomes complex, the fallback is to fetch the top-N candidates first (e.g., 50 by rank) and then filter in Python down to `limit`. This preserves performance characteristics and avoids deep SQL.

### Alternate: Persist `shared_position` at Write Time

Only if query-time filtering is not acceptable, compute and store `PlayerSimilarity.shared_position` at similarity-write time.

If doing this:
- Define `shared_position` explicitly as “position parent overlap” (not exact position code equality).
- Plan an explicit backfill for existing similarity rows.
- Accept that positions changing later can make stored values stale.

## Expected Regressions to Avoid

The recommendation above avoids:
- Breaking uniqueness constraints in `player_similarity`.
- Overwriting similarity rows due to snapshot-wide deletes.
- Doubling storage/compute for similarity rows.
- Changing the semantics of “Same Position Only” from a filter to a pool switch.

## Documentation Updates

Once implemented, update:
- `docs/same_position_similarity_plan.md` to reflect a filter-based fix (or deprecate it in favor of this doc).
- If needed, clarify in `docs/app-implementation/player_similarity_frontend.md` that “same position” means parent overlap, not necessarily exact `Position.code` match.

