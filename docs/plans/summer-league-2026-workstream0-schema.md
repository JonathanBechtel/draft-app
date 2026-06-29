# Workstream 0 — Born-canonical schema sketch (ticket-ready)

Concrete SQLModel definitions for the **two irreversible primitives** (Tier 0) that must
exist before any 2026 Summer League roster/stat row is written, plus the additive column
on the existing game-log table. Grounded in `docs/plans/global-player-journey-graph.md`
(§0 assertion-vs-projection, §3 layers, §5a/§5b affiliations-as-assertions, §7b
participation grain) and matched to the conventions in `app/schemas/summer_league.py`.

## Core modeling decision: split the assertion stream from the stat bridge

Two different jobs, two different tables — conflating them is what forces a later rewrite:

| Concern | Table | Layer | Mutability |
|---|---|---|---|
| **Roster assertion history** (announced → active → cut, who-said-what-when) | `player_affiliation` | universal hub | **append-only**; supersede, never overwrite |
| **Stable stat bridge** (the row game logs reference) | `summer_league_participation` | SL stat spoke | stable id per (player, team_entry, stint); summary fields updated in place |

- Game logs FK a **stable** `participation_id` — so they never need repointing.
- Roster churn writes **new** `player_affiliation` rows that supersede prior ones — so
  "announced July 1 vs. actually played" stays answerable forever (§5b).
- Per journey-graph §3, **affiliation is universal**; **participation is per-spoke**. So
  SL gets its own participation table by design — international/college get theirs later.
  Neither needs a cross-sport migration.

---

## 1. New universal table — `app/schemas/player_affiliation.py`

```python
"""Universal player-affiliation assertions (append-only, bitemporal).

An affiliation asserts that a player belonged to a team/program over an interval,
as learned from a source at a recorded time. Corrections supersede prior assertions
rather than mutating them, so historical answers never shift after a backfill.
See docs/plans/global-player-journey-graph.md §5b.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Column,
    Enum as SAEnum,
    Index,
    Text,
    text,
)
from sqlmodel import Field, SQLModel


class AffiliationType(str, Enum):
    """Scope of an affiliation (relaxes 'exactly one' — see journey-graph §7b)."""

    SUMMER_LEAGUE_ROSTER = "SUMMER_LEAGUE_ROSTER"
    # Reserved for later spokes (additive — no migration of SL rows):
    CLUB = "CLUB"
    NATIONAL_TEAM = "NATIONAL_TEAM"
    NBA_CONTRACT = "NBA_CONTRACT"
    COLLEGE = "COLLEGE"


class AffiliationStatus(str, Enum):
    """Lifecycle of a roster/affiliation assertion."""

    ANNOUNCED = "ANNOUNCED"   # named on a pre-event roster, no game yet
    CONFIRMED = "CONFIRMED"   # corroborated (e.g., appeared in a box score)
    ACTIVE = "ACTIVE"
    CUT = "CUT"               # dropped from a later roster pull
    WITHDRAWN = "WITHDRAWN"


class PlayerAffiliation(SQLModel, table=True):  # type: ignore[call-arg]
    """One append-only affiliation assertion for a canonical player."""

    __tablename__ = "player_affiliations"
    __table_args__ = (
        Index("ix_player_affiliations_player_id", "player_id"),
        Index("ix_player_affiliations_nba_team_id", "nba_team_id"),
        Index("ix_player_affiliations_status", "status"),
        Index("ix_player_affiliations_supersedes_id", "supersedes_id"),
        # Fast "current assertions" lookup — not yet superseded/retracted.
        Index(
            "ix_player_affiliations_active",
            "player_id",
            "nba_team_id",
            postgresql_where=text("superseded_at IS NULL AND retracted_at IS NULL"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    # Canonical player; nullable while a roster name is still unresolved
    # (a stub is created when --create-stubs is set, giving a non-null id).
    player_id: Optional[int] = Field(default=None, foreign_key="players_master.id")

    # Affiliation target. For SL the program is the NBA franchise; the generic
    # team/program FK is deferred (journey-graph §7a, §13) and lands additively
    # as a nullable column — SL rows are never repointed.
    nba_team_id: Optional[int] = Field(default=None, foreign_key="nba_teams.id")
    # team_program_id: reserved — added when the generic org model ships.

    affiliation_type: AffiliationType = Field(
        sa_column=Column(
            SAEnum(AffiliationType, name="affiliation_type_enum"),
            nullable=False,
        )
    )
    status: AffiliationStatus = Field(
        default=AffiliationStatus.ANNOUNCED,
        sa_column=Column(
            SAEnum(AffiliationStatus, name="affiliation_status_enum"),
            nullable=False,
            server_default=AffiliationStatus.ANNOUNCED.value,
        ),
    )

    # Bitemporal stamps (journey-graph §5b): effective_* = when true in the world;
    # recorded_at = when DraftGuru learned it; superseded_at/retracted_at = correction.
    effective_start: Optional[date] = Field(default=None)
    effective_end: Optional[date] = Field(default=None)
    recorded_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    supersedes_id: Optional[int] = Field(
        default=None, foreign_key="player_affiliations.id"
    )
    superseded_at: Optional[datetime] = Field(default=None)
    retracted_at: Optional[datetime] = Field(default=None)

    # Provenance — minimal now; Tier-1 assertion_evidence supersedes this pointer.
    source: str = Field(nullable=False)  # e.g. "nba_summer_league_roster"
    source_ref: Optional[str] = Field(default=None, sa_column=Column(Text))

    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
```

**Refresh semantics:** a roster pull never UPDATEs an existing assertion. A late add →
insert `ANNOUNCED`. A still-present player → no-op (already current). A drop → insert a
`CUT` assertion with `supersedes_id` = the prior row and set the prior row's
`superseded_at`. Box-score appearance → insert `CONFIRMED`.

---

## 2. New SL-spoke table — add to `app/schemas/summer_league.py`

```python
class SummerLeagueParticipation(SQLModel, table=True):  # type: ignore[call-arg]
    """Stable bridge: one row per (player, team_entry, stint) in a competition.

    Player game logs reference this row, not raw (player, edition). A stint
    captures a mid-competition team change or guest/replacement appearance
    (journey-graph §7b).
    """

    __tablename__ = "summer_league_participation"
    __table_args__ = (
        UniqueConstraint(
            "competition_id",
            "team_entry_id",
            "source_player_id",
            "stint_no",
            name="uq_summer_league_participation_comp_team_source_stint",
        ),
        Index("ix_summer_league_participation_player_id", "player_id"),
        Index(
            "ix_summer_league_participation_competition_team",
            "competition_id",
            "team_entry_id",
        ),
        Index(
            "ix_summer_league_participation_source_player_id",
            "source_player_id",
        ),
        Index("ix_summer_league_participation_affiliation_id", "affiliation_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    competition_id: int = Field(foreign_key="summer_league_competitions.id")
    team_entry_id: int = Field(foreign_key="summer_league_team_entries.id")
    source_player_id: int = Field(foreign_key="summer_league_source_players.id")
    # Backfilled on resolution, mirroring the game-log player_id pattern.
    player_id: Optional[int] = Field(default=None, foreign_key="players_master.id")
    # Current roster assertion for this participation (the append-only stream).
    affiliation_id: Optional[int] = Field(
        default=None, foreign_key="player_affiliations.id"
    )
    stint_no: int = Field(default=1, nullable=False)

    # Denormalized current roster state (the assertion history lives in
    # player_affiliations; this is the fast read).
    roster_status: AffiliationStatus = Field(
        default=AffiliationStatus.ANNOUNCED,
        sa_column=Column(
            SAEnum(AffiliationStatus, name="affiliation_status_enum"),
            nullable=False,
            server_default=AffiliationStatus.ANNOUNCED.value,
        ),
    )
    jersey_number: Optional[str] = Field(default=None)
    roster_position: Optional[str] = Field(default=None)
    first_game_date: Optional[date] = Field(default=None)
    last_game_date: Optional[date] = Field(default=None)
    games_played: Optional[int] = Field(default=None)

    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
```

> Import note: `summer_league.py` must import `AffiliationStatus` from
> `app.schemas.player_affiliation` (and Alembic auto-discovers both modules). The
> `affiliation_status_enum` SAEnum is created once (by the first table that ships it);
> in the migration, create the PG enum type a single time to avoid a duplicate-type error.

---

## 3. Additive column on the existing game-log table

`SummerLeaguePlayerGameLog` (`app/schemas/summer_league.py:430`) gains one nullable FK —
2026 rows populate it; pre-2026 rows stay null (backfillable later, additive):

```python
    # add alongside the existing player_id / source_player_id fields:
    participation_id: Optional[int] = Field(
        default=None, foreign_key="summer_league_participation.id"
    )
```

and a supporting index in `__table_args__`:

```python
    Index(
        "ix_summer_league_player_game_logs_participation_id",
        "participation_id",
    ),
```

---

## Migration plan (one Alembic revision)

1. **Create PG enum types** `affiliation_type_enum`, `affiliation_status_enum` (once).
2. **Create tables** `player_affiliations`, `summer_league_participation` via the
   new-table pattern (`SQLModel.metadata.create_all(bind=..., tables=[...])`), with all
   indexes/constraints above. Downgrade drops them and the enum types.
3. **Add column** `summer_league_player_game_logs.participation_id` (nullable) +
   its index via `op.add_column` / `op.create_index` (existing-table pattern — never
   drop/recreate the game-log table).

## Why this incurs no future rewrite of 2026 data

- **Grain is final:** game logs reference a stable `participation_id`; participation grain
  is `(player, team_entry, stint)` — the journey-graph's canonical grain. Generalizing the
  org/competition model later only *adds* parent FKs; it never repoints these rows.
- **History is preserved:** roster corrections are append-only assertions with bitemporal
  stamps — nothing is overwritten, so no information is lost that a later model would need.
- **Provenance is recoverable:** `source`/`source_ref` plus the retained raw snapshots
  (`SummerLeagueRawRun`/`RawFile` + `data/raw/...`) let Tier-1 `assertion_evidence` be
  backfilled without touching these rows.

## Deferred (additive when they land — explicitly NOT in Workstream 0 Tier 0)

- Generic `organization → team/program → team_entry` (§7a) — `player_affiliations` gains a
  nullable `team_program_id`; SL participation keeps its `team_entry_id`.
- Tier-1 `assertion_evidence` + `player_identity_action` audit (§6, §10).
- `player_lifecycle` reduction from affiliations, journey timeline, connection summaries
  (all replaceable projections — §5a, §8).
