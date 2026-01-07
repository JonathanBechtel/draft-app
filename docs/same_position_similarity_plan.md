# Bug Fix: "Same Position Only" Filter Returns No Results

## Problem Summary
The "Same Position Only" checkbox in Player Comparisons returns no results because position-only similarities are never computed.

## Solution Approach
Compute **separate similarity rows** for position-restricted comparisons using the same z-scores but limiting the nearest-neighbor pool to players sharing the same position parent group (guard, wing, forward, big).

## Files to Modify

| File | Change |
|------|--------|
| `app/schemas/metrics.py` | Add `same_position_only` field to `PlayerSimilarity` |
| `app/cli/compute_similarity.py` | Add position grouping logic and `--same-position-only` flag |
| `app/services/similarity_service.py` | Filter by `same_position_only` instead of `shared_position` |
| `alembic/versions/` | Migration to add new column |

---

## Part 1: Schema Changes

### 1.1 Add `same_position_only` to PlayerSimilarity
In `app/schemas/metrics.py`, add to `PlayerSimilarity`:

```python
same_position_only: bool = Field(
    default=False,
    description="True if similarity was computed within position group only",
)
```

### 1.2 Create Alembic migration
```bash
alembic revision --autogenerate -m "add same_position_only to player_similarity"
```

---

## Part 2: Compute Similarity Changes

### 2.1 Add position group fetching
Add to `compute_similarity.py`:

```python
from app.schemas.positions import Position

async def fetch_position_groups(session: AsyncSession) -> Dict[int, set[str]]:
    """Fetch player_id -> set of position parent groups.

    Uses Position.parents (JSONB array) to get groups like 'guard', 'wing', 'big'.
    """
    stmt = (
        select(PlayerStatus.player_id, Position.parents)
        .join(Position, Position.id == PlayerStatus.position_id)
        .where(PlayerStatus.position_id.is_not(None))
    )
    result = await session.execute(stmt)
    return {
        row.player_id: set(row.parents or [])
        for row in result.all()
    }
```

### 2.2 Add position-filtered feature frame builder
```python
def filter_frame_by_position(
    frame: pd.DataFrame,
    position_groups: Dict[int, set[str]],
) -> Dict[str, pd.DataFrame]:
    """Split feature frame by position group.

    Returns dict mapping group name -> DataFrame of players in that group.
    """
    group_to_players: Dict[str, List[int]] = defaultdict(list)
    for player_id in frame.index:
        groups = position_groups.get(player_id, set())
        for g in groups:
            group_to_players[g].append(player_id)

    return {
        group: frame.loc[frame.index.isin(players)]
        for group, players in group_to_players.items()
        if len(players) > 1  # Need at least 2 players to compare
    }
```

### 2.3 Add `--same-position-only` CLI flag
```python
parser.add_argument(
    "--same-position-only",
    action="store_true",
    help="Compute similarity within position groups only (guard, wing, big)",
)
```

### 2.4 Update write_similarity to accept position flag
Add `same_position_only: bool = False` parameter and set it on each `PlayerSimilarity` record:

```python
payload.append(
    PlayerSimilarity(
        snapshot_id=snapshot_id,
        dimension=dim,
        anchor_player_id=anchor,
        comparison_player_id=neighbor,
        similarity_score=sim,
        # ... other fields ...
        same_position_only=same_position_only,  # NEW
    )
)
```

### 2.5 Update compute_for_snapshot
When `--same-position-only` is set:
1. Fetch position groups
2. For each dimension frame, split by position group
3. Compute similarity within each group
4. Store results with `same_position_only=True`

```python
async def compute_for_snapshot(
    session: AsyncSession,
    snapshot: MetricSnapshot,
    config: SimilarityConfig,
    same_position_only: bool = False,
) -> None:
    metric_rows = await fetch_metric_rows(session, snapshot.id)
    frames = build_feature_frames(metric_rows)

    if same_position_only:
        position_groups = await fetch_position_groups(session)
        # For each dimension, compute similarity within each position group
        for dim, frame in frames.items():
            grouped_frames = filter_frame_by_position(frame, position_groups)
            for group_name, group_frame in grouped_frames.items():
                # Compute similarity within this group using same z-scores
                # but restricted player pool
                await write_similarity(
                    session, snapshot.id, {dim: group_frame}, config,
                    same_position_only=True
                )
    else:
        await write_similarity(session, snapshot.id, frames, config)
```

---

## Part 3: Service Layer Changes

### 3.1 Update similarity_service.py
Change the filter from `shared_position.is_(True)` to `same_position_only.is_(True)`:

```python
# In get_similar_players():
if same_position:
    stmt = stmt.where(PlayerSimilarity.same_position_only.is_(True))
```

Also add fallback logic: if no `same_position_only=True` rows exist, return empty (or the old behavior could be a warning).

---

## Part 4: Remove/Deprecate shared_position

The `shared_position` field becomes unnecessary with this design. Options:
- Leave it as-is (no harm, unused)
- Remove in a future cleanup migration

---

## Testing

1. Run `make precommit` and `mypy app --ignore-missing-imports`
2. Run similarity tests: `pytest tests/integration/test_similarity.py -v`
3. Manual test:
   - Compute similarity: `python -m app.cli.compute_similarity --snapshot-id <ID>`
   - Compute position-only: `python -m app.cli.compute_similarity --snapshot-id <ID> --same-position-only`
   - Verify both sets of rows exist in DB
   - Test UI checkbox switches between the two

## Deployment

1. Deploy migration and code changes
2. Run position-only computation for current snapshots:
```bash
python -m app.cli.compute_similarity --snapshot-id <ID> --same-position-only
```
