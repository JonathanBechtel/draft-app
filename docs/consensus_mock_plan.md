# Consensus Mock Draft & Big Board — Feature Plan

## Overview

Build a consensus ranking system that aggregates mock drafts and big boards
from existing Substack sources in our news feed. This becomes the **main
homepage feature** — the splash/hero section.

The feature has two analytics dimensions:
1. **Player analytics** — consensus rank, trend over time, range / volatility across sources
2. **Source/analyst analytics** — contrarian scores, deviation from consensus, who's early on risers

### Draft Calendar Behavior

- **Pre-lottery:** Homepage shows **Big Board Consensus** (pure talent ranking)
- **Post-lottery:** Homepage shows **Mock Draft Consensus** (pick-slot + team assignments)

Both views are powered by the same data plumbing; the homepage just renders a
different partial based on the calendar phase (and lets the visitor toggle).

---

## Big Board and Mock Draft — One Unified Schema

Earlier drafts of this plan treated Big Board and Mock Draft as fully separate
entities. After building the Big Board path end-to-end and inspecting the
overlap, we landed on a **single `Board` schema discriminated by `kind`** —
the data shapes and lifecycle rules diverge less than the conceptual framing
suggested.

### Big Board

An analyst's talent / value ranking of prospects, independent of team context.

- `kind = BIG_BOARD`
- Pure ordinal ranking: `BoardEntry.position` is the rank
- Optionally grouped into tiers (`BoardEntry.tier`) — stored for transcription
  fidelity but **ignored by the consensus algorithm** (admin-side metadata,
  not a consensus signal in v1)
- Can be any length (top 30, top 60, top 100)
- Published frequently
- Consensus question: **"Where does the average analyst rank this player by talent?"**

### Mock Draft

An analyst's prediction of what will actually happen on draft night.

- `kind = MOCK_DRAFT`
- Pick-slot driven, not talent-ranking driven
- `BoardEntry.position` is the overall pick number (1–60)
- Additional per-pick fields: `round` (1 or 2), `team_id` (selecting team),
  `original_team_id` (if traded), `trade_note`
- Two rounds, up to 60 picks
- Published less frequently, more labor-intensive
- Consensus questions:
  - **"Where is this player most commonly mocked?"** (player view)
  - **"Who does each team most commonly get?"** (team view — unique to mocks)

### Why unified rather than parallel tables

| Layer | Reused across both kinds |
|---|---|
| Schema | One `boards` table + one `board_entries` table with a few nullable mock-only columns |
| Service | One `board_service` with kind-aware validation in a few spots |
| Admin lifecycle | PENDING → APPROVED → REJECTED, autosave-on-blur, clone, reopen, edit metadata — identical |
| Admin UX patterns | Add-rows form (with conditional team picker for mocks), move up/down, sticky inputs |
| Tests | Lifecycle / immutability / autosave tests run once for both kinds |
| Operational scripts | `sync_from_prod` works for both unchanged |

What genuinely branches:

- **Consensus computation** has a player view for both kinds plus a team view
  for mocks only
- **Homepage display** is fundamentally different (ranked list vs pick-by-pick draft board)
- **A few validation rules** differ (mock requires `team_id`; big board allows `tier`)

Forcing a shared abstraction in the cases above would leak. Keeping the
**data model unified** and **branching only at the rendering and team-side
analytics layers** is the sweet spot.

---

## Data Model

### Source-of-truth tables

#### `Board`
| Field | Type | Notes |
|---|---|---|
| id | PK | |
| news_source_id | FK → NewsSource | The source / analyst |
| news_item_id | FK → NewsItem, nullable | Link to original article when known |
| draft_year | int | |
| published_at | datetime | When the analyst published it |
| size | int | Number of ranked players / picks |
| status | enum | PENDING / APPROVED / REJECTED |
| approved_at | datetime, nullable | Set on transition to APPROVED |
| kind | enum | BIG_BOARD / MOCK_DRAFT |
| num_rounds | int, nullable | MOCK_DRAFT only (1 or 2) |

#### `BoardEntry`
| Field | Type | Notes |
|---|---|---|
| id | PK | |
| board_id | FK → Board | ON DELETE CASCADE |
| player_id | FK → PlayerMaster | |
| position | int | Rank (big board) or pick number (mock draft) |
| tier | int, nullable | BIG_BOARD only |
| round | int, nullable | MOCK_DRAFT only |
| team_id | FK → Team, nullable | MOCK_DRAFT only (selecting team) |
| original_team_id | FK → Team, nullable | MOCK_DRAFT only (if traded) |
| trade_note | str, nullable | MOCK_DRAFT only |

A Postgres `CHECK` constraint enforces null/non-null rules:
- `kind = BIG_BOARD` → `round`, `team_id`, `original_team_id`, `trade_note` MUST be NULL
- `kind = MOCK_DRAFT` → `team_id` and `round` MUST NOT be NULL

Uniqueness:
- `UNIQUE (board_id, position)` — at most one player at each rank / pick
- `UNIQUE (board_id, player_id)` — same player can't appear twice on one board

### Consensus tables

The append-only snapshot model is generic across both kinds — the same
service produces a Big Board consensus snapshot for one draft_year and a
Mock Draft consensus snapshot for another. Tables landed in #200; the
service that fills them landed in #203.

#### `ConsensusSnapshot`
Groups one recompute pass for a `(draft_year, kind)` pair. Stores
`computed_at`, `num_boards`, the JSONB list of `board_ids`, and a `trigger`
enum (`BOARD_APPROVED` / `MANUAL` / `SCHEDULED`).

> **Future:** add a `kind` column to scope snapshots by kind so big-board
> snapshots and mock-draft snapshots are queryable independently. Not yet
> implemented — mock-draft consensus is a Slice 2c+ concern.

#### `BigBoardConsensus` *(name retained for now)*
Per-player row within a snapshot.
- `consensus_rank` (1-based final position)
- `avg_rank`, `median_rank`, `high_rank`, `low_rank`, `std_dev`, `num_sources`
- `prev_rank`, `rank_delta` — populated from the previous snapshot for the same year

For mock drafts, "rank" reads naturally as "pick" — the fields are
semantically generic but the column names date from the big-board-only
era. Renaming to `*_position` is a future low-risk refactor.

#### `SourceAnalytics`
Per-source row within a snapshot.
- `avg_deviation` (mean absolute distance from consensus across players
  this source ranked)
- `contrarian_score` (z-score of `avg_deviation` across sources in this snapshot)
- `biggest_outlier_player_id`, `outlier_delta` (signed)

One row per **eligible** source, even when MIN_SOURCES gates all that
source's players out of consensus (avg_deviation = 0 in that case).

#### *Future: `TeamConsensus`* (mock-draft only)
- `consensus_pick` per team — "Atlanta most commonly gets Player X at #5"
- `most_common_player_id`, `frequency_pct`
- Not yet implemented; added when mock-draft consensus is built out.

---

## Pipeline

### Ingest → Extract → Approve → Compute

1. **Ingest**: news feed fetches articles tagged `MOCK_DRAFT` or `BIG_BOARD`
2. **Extract** (manual for v1, AI later): structured extraction parses
   rankings from article content. Same `BoardEntry` shape regardless of
   kind; mock-draft entries carry the additional team/trade fields.
3. **Admin approve**: extracted board lands in a PENDING review queue; admin
   approves / edits / rejects via `/admin/boards`.
4. **Recompute consensus**: on approval, `consensus_service.recompute_consensus`
   fires automatically (transaction-scoped — if recompute fails, approval
   rolls back).

### Why admin approval

- Prevents misrepresenting an analyst's rankings if extraction is wrong
- Low volume (~5–10 new boards per week across all sources) makes it feasible
- Approval UI shows extracted board side-by-side with original article

### Extraction Strategy — Single Extractor with Per-Source Hints

Different substacks format boards differently — numbered lists, tier
headers, tables, prose with embedded rankings. The decision: **one shared
LLM extractor that pulls per-source hints from `NewsSource` at call time**
(via an `extraction_hints` JSON column).

```json
{
  "format": "tier_list",
  "tier_labels": "Tier 1..Tier 5",
  "rank_within_tier_is_implicit": true
}
```

The shared prompt injects those hints so the model gets source-specific
context without per-source code. New sources are added by writing hints,
not code; the admin approval queue catches errors before any extracted
board affects consensus.

**Deferred to a later phase** — manual admin entry is what we have for v1.

### Manual entry as the v1 primary path

Until AI extraction lands, admins enter boards via `/admin/boards`:
- Create empty PENDING board with source / draft year / published_at / kind
- Add entries one at a time with player autocomplete (and team picker for mocks)
- Move up/down arrows reorder ranks/picks
- Tier values pre-fill from the prior entry (sticky)
- Edit details (source/year/date) and individual entries while PENDING
- Clone an APPROVED board into a fresh PENDING copy for the next iteration
- Reopen an APPROVED board → PENDING if you misclicked Approve
- Approve, Reject (audit-preserving), or Delete (PENDING only)

---

## Player Analytics (derived from consensus)

- **Consensus rank + trend**: current position and rank_delta against previous snapshot
- **Range / volatility**: high–low spread and std_dev — "how settled is this player?"
- **Agreement zones**: "all 6 sources have Flagg top 2" vs "Bailey ranges from 2 to 8"
- **Historical trajectory**: `get_player_rank_history` returns oldest-first snapshot rows
- **Source breakdown per player**: each source's rank for a given player

## Source / Analyst Analytics

- **Contrarian score**: z-scored avg_deviation across this snapshot's sources
- **Biggest outlier**: which player does this source diverge on most? Signed `outlier_delta`
- **Early-mover detection**: which source had a riser ranked high before consensus caught up (future)
- **Source-vs-source comparison**: for any player, see all sources side by side
- **Accuracy tracking**: compare to actual draft results post-draft (future)

---

## Homepage Design

The consensus board becomes the **main hero / splash** of the homepage.

### Hero section
- Full top-30+ consensus table for the current draft year
- Each row: rank, rank_delta arrow, player name + school, avg rank, range (high–low), # sources
- Click row → player detail page
- Visual indicators: risers (green), fallers (red), new entries
- **Tab toggle** between Big Board and Mock Draft views; default determined by
  draft calendar phase (pre-lottery → Big Board, post-lottery → Mock Draft).
  Override via `?view=board` / `?view=mock` for direct linking.

### Supporting panels
- **Biggest movers**: top 3–5 risers and fallers with deltas
- **Source spotlight**: "Most contrarian source this week: [Source] — avg deviation X.X"
- **Board freshness**: "Based on N boards from M sources, last updated [date]"
- Existing homepage content (news feed, trending, podcasts) shifts below the consensus hero

### Player detail integration
- "Consensus rank: #X" on player pages
- Source-by-source breakdown for that player
- Rank-history line chart (uses `get_player_rank_history`)

### Source / analyst page
- Leaderboard of sources by contrarian score / consensus alignment
- Per-source: their current board vs consensus overlay
- Biggest outlier picks per source

---

## Implementation Phases

### Phase 1: Schema & Data Entry Pipeline ✅

- ✅ Unified `Board` / `BoardEntry` schema with kind discriminator (this PR — was originally landed as separate `big_boards` tables in #192 and renamed here)
- ✅ Admin manual entry UI for big boards (#194, #196, #197, #198)
- ✅ Operational sync script (#199)
- ⏳ Mock-draft mode for admin entry (kind=MOCK_DRAFT path through the same UI with team picker + trade fields exposed)
- ⏳ AI extraction prompts + per-source hints

### Phase 2: Consensus Computation Engine

- ✅ `ConsensusSnapshot` / `BigBoardConsensus` / `SourceAnalytics` schemas with uniqueness constraints (#200)
- ✅ `recompute_consensus` service + `approve_board` trigger hook (#203)
- ⏳ Slice 2c: Admin "Recompute now" button + `/admin/consensus` view page
- ⏳ Slice 2d: Daily cron as safety-net trigger
- ⏳ `TeamConsensus` table + per-team aggregation (mock-draft only)
- ⏳ Add `kind` column to `ConsensusSnapshot` to scope by kind

### Phase 3: Homepage Redesign

- Consensus hero section replacing the current splash
- Big Board / Mock Draft tab toggle with phase-aware default
- Biggest movers panel (uses `rank_delta`)
- Source spotlight widget (uses `source_analytics.contrarian_score`)
- Board freshness indicator

### Phase 4: Player Detail & Source Analytics Pages

- Consensus rank + source breakdown on player detail page
- Rank-history visualization
- Dedicated source / analyst analytics page
- Source comparison tools

---

## Design Decision Log

Major decisions made along the way, with the reasoning:

- **Tier is admin-side only** (not aggregated): different sources use tier
  inconsistently; aggregating tiers across sources is noise. Stored on
  `BoardEntry` for transcription fidelity, ignored by `consensus_service`.
- **Unified Board schema with kind discriminator** (this PR): see "Big Board
  and Mock Draft — One Unified Schema" above. Decision reached after building
  the Big Board path end-to-end and seeing the overlap was much larger than
  the conceptual framing suggested.
- **Append-only consensus snapshots**: free rank-trajectory chart, no race
  conditions, ~11k rows per year per kind is negligible.
- **`MIN_SOURCES = 2` floor**: module constant for now; revisit as a
  per-snapshot column or majority-based rule once we have 5+ sources.
- **Manual entry first, AI later**: validates data model with real entry
  workflow before investing in extraction infrastructure.
- **Source matched by name, players by slug** in the sync script: prod IDs
  never leak into dev; missing sources are auto-created.
