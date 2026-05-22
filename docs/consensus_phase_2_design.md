# Phase 2 — Consensus Computation Engine

Design notes for the consensus layer that turns approved per-source big
boards into a single ranked board with volatility / agreement signals,
plus source-level analytics. Builds directly on the Phase 1 schemas
(`big_boards`, `big_board_entries`) and lifecycle (PENDING → APPROVED).

This document captures the design intent before implementation; revise
once we've entered the first 4–6 real boards (see seed list in
`consensus_mock_plan.md`).

---

## What Phase 2 Adds

Two tables and one stateless service:

1. **`big_board_consensus`** — per-player ranked summary across all
   APPROVED boards for a given draft year, with delta-from-previous
   tracking for risers/fallers.
2. **`source_analytics`** — per-source metrics quantifying how
   contrarian each analyst is and which player they diverge most on.
3. **`big_board_consensus_service`** — recomputes both tables on
   approval (or on demand).

The homepage hero in Phase 3 reads only from the consensus tables, not
from per-source entries. The per-source `big_boards` rows remain the
auditable source of truth.

---

## Data Model

### `big_board_consensus`

One row per `(snapshot_id, player_id)`. Snapshots are append-only so we
can chart rank trajectories over time.

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `snapshot_id` | FK → `consensus_snapshots.id` | Groups all rows from one computation pass |
| `draft_year` | int, indexed | |
| `player_id` | FK → `players_master.id`, indexed | |
| `consensus_rank` | int | Final position (1-based) on the consensus board |
| `avg_rank` | float | Mean across sources that ranked this player |
| `median_rank` | float | Median across sources |
| `high_rank` | int | Best (lowest) rank any source gave this player |
| `low_rank` | int | Worst (highest) rank any source gave |
| `std_dev` | float | Std dev of ranks — volatility / agreement signal |
| `num_sources` | int | How many of the eligible boards ranked this player |
| `prev_rank` | int, nullable | `consensus_rank` from the previous snapshot |
| `rank_delta` | int, nullable | `prev_rank − consensus_rank` (positive = rising) |

Composite indexes: `(snapshot_id, consensus_rank)` for "show me snapshot
N's top 30", `(player_id, snapshot_id)` for "player rank history".

### `consensus_snapshots`

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `draft_year` | int, indexed | |
| `computed_at` | datetime, indexed | When this snapshot was computed |
| `num_boards` | int | How many APPROVED boards fed this snapshot |
| `board_ids` | JSONB | List of `big_boards.id` included (audit) |
| `trigger` | enum | `BOARD_APPROVED` / `MANUAL` / `SCHEDULED` |

Lets us answer "which sources fed the 2026-05-15 snapshot?" without
joining through entries.

### `source_analytics`

One row per `(snapshot_id, source_id)`.

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `snapshot_id` | FK → `consensus_snapshots.id` | |
| `news_source_id` | FK → `news_sources.id` | |
| `latest_board_id` | FK → `big_boards.id` | Which board of theirs was used |
| `avg_deviation` | float | Mean absolute rank-distance from consensus across the players this source ranked |
| `contrarian_score` | float | Normalized version of `avg_deviation` (z-score across sources) |
| `biggest_outlier_player_id` | FK → `players_master.id`, nullable | Where this source diverges most |
| `outlier_delta` | int | Signed delta: positive = source higher than consensus |

Joined back to source name for display.

---

## Computation Algorithm

### Snapshot composition

For a given `draft_year`, pick **the most recent APPROVED board per
source**. This is the "current state" view — historical boards from the
same source don't double-count.

```
eligible_boards = SELECT DISTINCT ON (news_source_id) *
                  FROM big_boards
                  WHERE status = 'APPROVED' AND draft_year = :year
                  ORDER BY news_source_id, published_at DESC
```

Open question: do we eventually want a "rolling 30-day window" view
where a source's older board counts if they haven't published a new
one? Defer until we see real cadence.

### Per-player aggregation

For each player who appears on ≥ 1 eligible board:

```python
ranks = [entry.rank for entry in entries_for_player_across_boards]
avg = mean(ranks)
median = statistics.median(ranks)
high = min(ranks)
low = max(ranks)
std_dev = statistics.stdev(ranks) if len(ranks) > 1 else 0.0
num_sources = len(ranks)
```

Then sort all players by `avg_rank` ascending; assign `consensus_rank`
= 1-based position in that ordering.

**Tie-breaker**: when two players share the same `avg_rank`, fall back
to `median_rank`, then `high_rank`, then `player_id` (stable).

**Min-sources floor**: don't include players ranked by fewer than
`MIN_SOURCES` boards on the consensus output. Start with
`MIN_SOURCES = 2` so a single contrarian source can't seed a top-30
slot on their own. Revisit once we have 5+ boards.

### Per-source analytics

For each eligible board:

```python
deviations = []
for entry in board.entries:
    consensus = consensus_rank_for(entry.player_id)
    if consensus is None:  # player didn't clear MIN_SOURCES
        continue
    deviations.append(abs(entry.rank - consensus))

avg_deviation = mean(deviations)
biggest_outlier = max by abs(entry.rank - consensus_rank_for(entry.player_id))
outlier_delta = consensus_rank − source_rank   # positive = source higher
```

`contrarian_score` is a z-score of `avg_deviation` across all sources
in the same snapshot, so it stays meaningful as more sources come on.

### Delta tracking

For each `(player_id, snapshot)`, look up the **previous snapshot for
the same `draft_year`** and copy its `consensus_rank` into `prev_rank`.
`rank_delta = prev_rank - consensus_rank` (positive = rising, since
lower rank number is better).

For players newly entering the consensus: `prev_rank = NULL`,
`rank_delta = NULL`. Phase 3 can render those as "NEW".

---

## Triggers

Three ways to recompute:

1. **`BOARD_APPROVED`** — fire from `approve_board` in
   `big_board_service`. Cost: one snapshot per approval. Acceptable at
   our volume (≤ 10 approvals per week).
2. **`MANUAL`** — admin button on `/admin/big-boards`: "Recompute now".
   Useful after correcting a stale board or backfilling.
3. **`SCHEDULED`** — daily cron via Fly's release schedule. Catches any
   missed triggers and gives us at least one snapshot per day even
   during quiet periods, so the rank-history chart has continuous
   points.

Computation cost: each pass is `O(boards × entries)` plus an `O(n²)`
sort — trivial for our N (≤ 10 boards × 60 entries).

---

## Snapshot Strategy

**Append-only snapshots.** Each compute pass writes a new
`consensus_snapshots` row and a fresh set of `big_board_consensus` /
`source_analytics` rows. We never UPDATE existing snapshot rows.

Pros:
- Trivial historical replay ("what did consensus look like on 2026-05-15?")
- Trend charts come for free — just `WHERE player_id = X` ordered by snapshot date
- No race conditions if two approvals fire near-simultaneously

Cons / mitigations:
- Storage: ~30 rows per snapshot × daily snapshots × 1 year ≈ 11k rows. Negligible.
- Cleanup: prune snapshots older than the previous draft year on each compute pass, OR keep everything for cross-class analysis (preferred).

**Query pattern for "current consensus":**
```sql
SELECT * FROM big_board_consensus
WHERE snapshot_id = (
  SELECT id FROM consensus_snapshots
  WHERE draft_year = :year
  ORDER BY computed_at DESC LIMIT 1
)
ORDER BY consensus_rank;
```

Add an index on `consensus_snapshots(draft_year, computed_at DESC)` to
make that subquery instant.

---

## Service Interface (sketch)

```python
# app/services/consensus_service.py

async def recompute_consensus(
    db: AsyncSession,
    *,
    draft_year: int,
    trigger: ConsensusTrigger,
) -> ConsensusSnapshot:
    """Run a full recompute for one draft year. Returns the new snapshot row.

    Picks the most recent APPROVED board per source, aggregates by player,
    writes a new snapshot + per-player + per-source rows in a single
    transaction. Idempotent on the (draft_year, computed_at) tuple.
    """

async def get_latest_consensus(
    db: AsyncSession,
    *,
    draft_year: int,
    limit: int | None = None,
) -> list[BigBoardConsensus]:
    """Return the most recent snapshot's consensus rows, ordered by rank."""

async def get_player_rank_history(
    db: AsyncSession,
    *,
    player_id: int,
    draft_year: int,
) -> list[BigBoardConsensus]:
    """Return all snapshot rows for one player, ordered chronologically."""
```

Routes are out of scope for Phase 2 — they're a Phase 3 concern
(homepage hero + player detail integration).

---

## Open Questions

1. **`MIN_SOURCES` floor**: 2 is a reasonable starting point but
   essentially excludes deep prospects ranked by only one source. Maybe
   surface a separate "fringe consensus" view in admin?
2. **Source weighting**: should we eventually weight sources by
   historical accuracy (post-draft) or stay strictly equal-weight?
   Defer until we have one draft cycle of post-draft data.
3. **International / G League prospects** that only some boards
   include: same question as #1 — the `MIN_SOURCES` floor handles the
   trivial case but may hide signal.
4. **Tier-aware aggregation**: the schema already stores `tier`, but
   the v1 algorithm ignores it. Worth revisiting once we see how
   sources actually use tiers.
5. **Pre-lottery vs. post-lottery**: the existing plan says we switch
   the homepage hero from BigBoard consensus to MockDraft consensus
   after the lottery. That's a Phase 3 decision but worth flagging
   here since it affects what we surface from this engine year-round.

---

## Implementation Phasing

Suggested slices for the eventual Phase 2 PRs:

- **Slice 2a** — schemas + migration (`consensus_snapshots`,
  `big_board_consensus`, `source_analytics`)
- **Slice 2b** — `recompute_consensus` service + unit/integration
  tests; trigger hook in `approve_board`
- **Slice 2c** — admin "Recompute now" button + simple
  `/admin/consensus` page showing the latest snapshot in tabular form
  (no public route yet)
- **Slice 2d** — scheduled cron

Defer Slice 2c/2d until at least 2b is in and we have real boards
flowing through.
