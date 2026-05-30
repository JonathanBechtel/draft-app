# Draft-Order Reference & Post-Lottery Mock Presentation

**Status:** Specced, not started. Downstream of #228 (mock ranking ingestion, merged).
**Project:** #2 (Consensus Mock Draft & Big Board). Master reference: #207.

## Motivation

DraftGuru treats big boards and mock drafts as **one pool of player rankings** at
the data layer: a board is an ordered ranking of prospects by NBA-success
likelihood, and the consensus engine aggregates every approved board by
`position` regardless of `kind` (see `consensus_service._select_eligible_boards`,
which never filters on `kind`). The big-board-vs-mock-draft distinction is a
**presentation concern**, switched by calendar phase via
`config.get_consensus_board_kind()` around `LOTTERY_DATE`.

What makes a mock-draft *presentation* different from a big-board presentation is
not the ranking — it's the **team at each pick**. That pick ownership is
**canonical, static, public data**: once the lottery sets the order, slot 1 →
Team A, slot 2 → Team B, … plus traded-pick reassignments. It is identical for
every analyst's mock, so inferring it per-article (as the original #228 scoped)
is wasted, error-prone work. Instead we maintain it **once**, as a reference
table, and overlay it on the consensus ranking at render time.

> Pick ownership encodes team need/fit, so a mock's pick order is a noisier
> talent signal than a big board. That noise is accepted on purpose (more data;
> see the bottom-up-metrics philosophy). This ticket does **not** change the
> consensus math — it only adds the team overlay for the post-lottery view.

## End-to-end vision (plan first, then prune)

### 1. `DraftPickSlot` reference table (`app/schemas/draft_pick_slots.py`)

Canonical pick order for a draft year, one row per overall pick.

| column | type | notes |
|---|---|---|
| `id` | PK | |
| `draft_year` | int, indexed | |
| `overall_pick` | int | 1-based; unique with `draft_year` |
| `round` | int | 1 or 2 |
| `round_pick` | int | pick number within the round (1..30) |
| `team_id` | FK → `nba_teams.id` | current owner of the pick |
| `original_team_id` | FK → `nba_teams.id`, nullable | original owner if traded |
| `trade_note` | str, nullable | e.g. "via trade with PHX" |
| `created_at` / `updated_at` | datetime | |

Constraints: `UniqueConstraint(draft_year, overall_pick)`,
`UniqueConstraint(draft_year, round, round_pick)`. New table → create via
`SQLModel.metadata.create_all(..., tables=[DraftPickSlot.__table__])` in the
Alembic upgrade (drop in downgrade), per the repo migration convention. Add the
module to `tests/integration/conftest.py` schema imports.

### 2. Service layer (`app/services/draft_order_service.py`)

- `get_draft_order(db, *, draft_year) -> list[DraftPickSlot]` — ordered by
  `overall_pick`; the read primitive for the presentation join.
- `upsert_pick_slot(db, *, draft_year, overall_pick, round, round_pick, team_id, original_team_id=None, trade_note=None)` — idempotent insert/update for admin + seed.
- `bulk_replace_draft_order(db, *, draft_year, slots)` — replace a year's order wholesale (used by the seed script / "paste the official order" admin action).

### 3. Admin populate path

- Route subpackage `app/routes/admin/draft_order.py` mounted at
  `/admin/draft-order` (matches the `app/routes/admin/` subpackage layout —
  NOT a flat module).
  - `GET /admin/draft-order?draft_year=YYYY` — editable grid of the 60 slots
    with a team `<select>` per row plus traded-from + trade-note fields.
  - `POST .../save` — persist the grid via `bulk_replace_draft_order`.
- Template `app/templates/admin/draft-order/index.html`.
- A seed script `scripts/seed_draft_order.py` to load a known year's order from a
  small CSV/JSON (the 2026 order is public) so the data exists without manual
  entry. Mirror an existing `scripts/run_*` / seed-script convention; load env
  via `scripts/with-db-env.sh`.

### 4. Presentation join (the payoff)

Extend `consensus_read_service` so the post-lottery view maps the consensus
ranking onto the draft order:

- `get_mock_consensus_board(db, *, draft_year, snapshot_id=None) -> list[MockConsensusRow]`
  — calls `get_consensus_board` (the existing unified consensus), then joins
  `consensus_rank N → draft slot N → owning team`. Each row carries the player +
  consensus stats (reused from `ConsensusRow`) plus `team`, `original_team`,
  `trade_note`, `round`. Picks beyond the consensus list (or consensus entries
  beyond the slot count) degrade gracefully.
- Wire it into the calendar-aware surfaces (homepage hero + any
  `/consensus`-style page) so that **after** `LOTTERY_DATE` the same consensus
  renders as a mock draft with team chips/logos, and **before** it renders as a
  big board. Reuse `school_logo_service` / team `logo_url` for team art.

### 5. Cleanup folded into this ticket

Now-vestigial under the unified model:

- Drop the `BoardEntry` per-pick columns `team_id`, `original_team_id`,
  `round`, `trade_note` (never populated after #228; ownership lives in
  `DraftPickSlot`). Alembic `op.drop_column` ×4 + downgrade re-add.
- Drop the unused `MockDraftConsensus` schema/table (consensus is unified;
  the engine only ever writes `BigBoardConsensus`).
- Consider renaming `BigBoardConsensus` → a neutral `ConsensusEntry` to reflect
  that it's the unified pool, not a big-board-only table. Larger blast radius
  (schema + migration + `consensus_service` + `consensus_read_service` +
  templates + tests) — keep it a clearly-scoped sub-step or split to its own PR
  if the diff gets unwieldy.
- Revisit `Board.num_rounds` + the `ck_boards_kind_num_rounds` constraint: rounds
  are a property of the draft year (now in `DraftPickSlot`), so `num_rounds` on
  `Board` is approximate metadata. Decide whether to keep it as a provenance hint
  or drop it.

## Prune guidance

Minimum shippable slice if scope must shrink: **§1 table + §2 service + §3 seed
script**, which lands the benchmark data. §4 (presentation join) can follow once
the calendar nears the post-lottery mock phase. §5 cleanup can be its own PR.

## Verification

- Integration: `DraftPickSlot` uniqueness constraints; `get_draft_order` ordering;
  `upsert`/`bulk_replace` idempotency; the consensus→slot→team join (including
  traded picks and graceful degradation when counts mismatch).
- e2e/visual: admin grid edit + save; post-lottery homepage renders team chips on
  the consensus; pre-lottery renders the plain big board (calendar toggle).
- Migration: `alembic upgrade head` then `downgrade base` round-trips cleanly,
  including the §5 column drops/re-adds.
