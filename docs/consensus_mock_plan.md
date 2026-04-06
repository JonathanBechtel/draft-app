# Consensus Mock Draft & Big Board — Feature Plan

## Overview

Build a consensus ranking system that aggregates mock drafts and big boards from existing Substack sources in our news feed. This becomes the **main homepage feature** — the splash/hero section.

The feature has two analytics dimensions:
1. **Player analytics** — consensus rank, trend over time, range/volatility across sources
2. **Source/analyst analytics** — contrarian scores, deviation from consensus, who's early on risers

### Draft Calendar Behavior

- **Pre-lottery:** Homepage shows **Big Board Consensus** (pure talent ranking)
- **Post-lottery:** Homepage shows **Mock Draft Consensus** (pick-slot + team assignments)

These are structurally different entities — not the same thing with different labels.

---

## Big Board vs Mock Draft — Why They're Separate

### Big Board

An analyst's talent/value ranking of prospects, independent of team context.

- Pure ordinal ranking: player + rank position
- Optionally grouped into tiers
- Can be any length (top 30, top 60, top 100)
- Published frequently, updated as the season progresses
- Consensus question: **"Where does the average analyst rank this player by talent?"**

### Mock Draft

An analyst's prediction of what will actually happen on draft night.

- Pick-slot driven, not talent-ranking driven
- Each entry is a **pick** assigned to a **team** and a **player**
- Two rounds, up to 60 picks
- Includes traded picks (selecting team ≠ original pick owner)
- Can include trade scenario notes
- Published less frequently, more labor-intensive
- Consensus question: **"Where is this player most commonly mocked?"** AND **"Who does each team most commonly get?"**

---

## Data Model

### Raw Source Data

#### `BigBoard`
| Field | Type | Notes |
|-------|------|-------|
| id | PK | |
| news_source_id | FK → NewsSource | The source/analyst |
| news_item_id | FK → NewsItem, nullable | Link to original article if applicable |
| draft_year | int | |
| published_at | datetime | When the board was published |
| board_size | int | Number of players ranked |
| status | enum | PENDING / APPROVED / REJECTED |

#### `BigBoardEntry`
| Field | Type | Notes |
|-------|------|-------|
| id | PK | |
| board_id | FK → BigBoard | |
| player_id | FK → PlayerMaster | |
| rank | int | Analyst's talent ranking position |
| tier | int, nullable | Optional tier grouping |

#### `MockDraft`
| Field | Type | Notes |
|-------|------|-------|
| id | PK | |
| news_source_id | FK → NewsSource | The source/analyst |
| news_item_id | FK → NewsItem, nullable | Link to original article if applicable |
| draft_year | int | |
| published_at | datetime | When the mock was published |
| num_rounds | int | Number of rounds covered (1 or 2) |
| status | enum | PENDING / APPROVED / REJECTED |

#### `MockDraftPick`
| Field | Type | Notes |
|-------|------|-------|
| id | PK | |
| mock_id | FK → MockDraft | |
| player_id | FK → PlayerMaster | |
| pick_number | int | Overall pick (1–60) |
| round | int | 1 or 2 |
| team_id | FK → Team | Team making the selection |
| original_team_id | FK → Team, nullable | Original pick owner if traded |
| trade_note | str, nullable | E.g., "via trade with PHX" |

### Computed / Consensus Data

#### `BigBoardConsensus`
| Field | Type | Notes |
|-------|------|-------|
| id | PK | |
| snapshot_date | date | When consensus was computed |
| draft_year | int | |
| player_id | FK → PlayerMaster | |
| consensus_rank | int | Final consensus position |
| avg_rank | float | Mean rank across sources |
| median_rank | float | Median rank |
| high_rank | int | Best (lowest number) rank from any source |
| low_rank | int | Worst (highest number) rank from any source |
| std_dev | float | Standard deviation — volatility measure |
| num_sources | int | How many boards include this player |
| prev_rank | int, nullable | Previous snapshot's consensus rank |
| rank_delta | int, nullable | Change from previous snapshot |

#### `MockDraftConsensus`
| Field | Type | Notes |
|-------|------|-------|
| id | PK | |
| snapshot_date | date | When consensus was computed |
| draft_year | int | |
| player_id | FK → PlayerMaster | |
| consensus_pick | int | Final consensus pick slot |
| avg_pick | float | Mean pick across sources |
| median_pick | float | Median pick |
| high_pick | int | Earliest (lowest number) pick from any source |
| low_pick | int | Latest (highest number) pick from any source |
| std_dev | float | |
| most_common_team_id | FK → Team, nullable | Team most frequently linked |
| team_frequency_pct | float, nullable | % of mocks linking player to that team |
| num_sources | int | |
| prev_pick | int, nullable | |
| pick_delta | int, nullable | |

#### `SourceAnalytics`
| Field | Type | Notes |
|-------|------|-------|
| id | PK | |
| news_source_id | FK → NewsSource | |
| snapshot_date | date | |
| board_type | enum | BIG_BOARD / MOCK_DRAFT |
| avg_deviation | float | Mean distance from consensus across all players |
| contrarian_score | float | Normalized contrarian metric |
| biggest_outlier_player_id | FK → PlayerMaster | Player where this source deviates most |
| outlier_delta | int | How far off consensus for that player |

---

## Pipeline

### Ingest → Extract → Approve → Compute

1. **Ingest**: Existing news feed fetches articles tagged `MOCK_DRAFT` or `BIG_BOARD`
2. **AI Extract**: Structured extraction parses rankings from article content
   - Big board parser: looks for ordered lists of players → `[{player, rank, tier?}]`
   - Mock draft parser: looks for pick-team-player triples → `[{pick, round, team, player, trade_note?}]`
   - Different prompts, different validation for each type
3. **Admin Approve**: Extracted board lands in a pending review queue; admin approves/edits/rejects
4. **Recompute Consensus**: On approval, consensus snapshots recompute immediately from all approved boards

### Why Admin Approval?

- Prevents misrepresenting an analyst's rankings if AI extraction is wrong
- Low volume (~5–10 new boards per week across all sources) makes this feasible
- Approval UI can show extracted board side-by-side with original article for quick verification

---

## Player Analytics (derived from consensus)

- **Consensus rank + trend**: current position and trajectory over time (risers/fallers)
- **Range / volatility**: high-low spread and std_dev — "how settled is this player?"
- **Agreement zones**: "all 6 sources have Flagg top 2" vs "Bailey ranges from 2 to 8"
- **Historical trajectory**: "Player X has risen from #18 to #7 over 6 weeks"
- **Source breakdown per player**: show each source's rank for a given player

## Source/Analyst Analytics (the unique twist)

- **Contrarian score**: average deviation from consensus — who's the most/least contrarian?
- **Biggest outlier**: which player does this source diverge on most?
- **Early mover detection**: which source had a riser ranked high before consensus caught up
- **Source-vs-source comparison**: for any player, see all sources side by side
- **Archetype tendencies**: "this source tends to be high on international prospects" (future)
- **Accuracy tracking**: compare to actual draft results post-draft (future)

---

## Homepage Design

The consensus board becomes the **main hero/splash** of the homepage.

### Hero Section — "2026 Consensus Board" (or "2026 Mock Draft" post-lottery)

- Full top-30+ consensus ranking table
- Each row: rank, rank delta (arrow), player name + school, avg rank, range (high–low), # sources
- Click row → player detail page
- Visual indicators: risers (green), fallers (red), new entries
- Board type adapts automatically based on draft calendar phase

### Supporting Panels

- **Biggest Movers**: top 3–5 risers and fallers with deltas
- **Source Spotlight**: "Most contrarian source this week: [Source] — avg deviation X.X"
- **Board Freshness**: "Based on N boards from M sources, last updated [date]"
- **Existing homepage content** (news feed, trending, podcasts) shifts below the consensus hero

### Player Detail Integration

- "Consensus rank: #X" on player pages
- Source-by-source breakdown for that player
- Rank history chart over time

### Source/Analyst Page

- Leaderboard of sources by contrarian score / consensus alignment
- Per-source: their current board vs consensus overlay
- Biggest outlier picks

---

## Implementation Phases

### Phase 1: Schema & Data Entry Pipeline
- Create `BigBoard`, `BigBoardEntry`, `MockDraft`, `MockDraftPick` tables + migrations
- AI extraction prompts (separate for big board vs mock draft)
- Admin approval queue UI (pending boards list, side-by-side review, approve/edit/reject)

### Phase 2: Consensus Computation Engine
- `BigBoardConsensus` and `MockDraftConsensus` tables + migrations
- Consensus computation service (triggered on board approval)
- `SourceAnalytics` table + computation
- Historical snapshot tracking (prev_rank / rank_delta)

### Phase 3: Homepage Redesign
- Consensus hero section replacing current top-of-page layout
- Biggest movers panel
- Source spotlight widget
- Draft calendar–aware board type toggle
- Board freshness indicator

### Phase 4: Player Detail & Source Analytics Pages
- Consensus rank + source breakdown on player detail page
- Rank history visualization
- Dedicated source/analyst analytics page
- Source comparison tools
