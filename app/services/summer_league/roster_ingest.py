"""Idempotent roster loader: turns parsed ``RosterEntry`` records into canonical rows.

The loader writes append-only ``PlayerAffiliation`` assertions and stable
``SummerLeagueParticipation`` bridge rows. Corrections supersede prior
assertions rather than mutating them. Emits a ``RosterDiffReport`` on every call.

Critical invariant
------------------
The assertion history in ``player_affiliations`` is **append-only**: rows are
never deleted or overwritten. A dropped player triggers a new ``CUT`` assertion
that points at the prior ``ANNOUNCED`` row via ``supersedes_id``; the prior row
gains a ``superseded_at`` timestamp but is otherwise untouched. This lets callers
answer point-in-time questions from the assertion stream alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_affiliation import (
    AffiliationStatus,
    AffiliationType,
    PlayerAffiliation,
)
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueParticipation,
    SummerLeagueResolutionStatus,
    SummerLeagueSourceRecord,
    SummerLeagueTeamEntry,
)
from app.services.player_mention_service import _normalized_name_key
from app.services.summer_league.roster_parse import RosterEntry


# ---------------------------------------------------------------------------
# Public data contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompetitionKey:
    """Minimal key to look up or create a ``SummerLeagueEdition`` row.

    Args:
        year: Summer League season year (e.g. 2026).
        league_id: NBA Stats LeagueID string (``"15"``, ``"13"``, ``"16"``).
        venue_slug: Canonical venue slug (e.g. ``"las_vegas"``).
    """

    year: int
    league_id: str
    venue_slug: str


@dataclass
class TeamDiff:
    """Roster diff counts for one NBA Stats team.

    Args:
        added: New players not previously seen on this team's roster.
        unchanged: Players present in both the current and incoming roster.
        cut: Players present in the current roster but absent from the incoming pull.
    """

    added: int = 0
    unchanged: int = 0
    cut: int = 0


@dataclass
class RosterDiffReport:
    """Per-team and total roster diff counts for one ``load_roster_snapshot`` call.

    Args:
        per_team: Diff counts keyed by ``nba_stats_team_id``.
        added: Total players newly added across all teams.
        unchanged: Total players unchanged across all teams.
        cut: Total players cut across all teams.
    """

    per_team: dict[str, TeamDiff] = field(default_factory=dict)
    added: int = 0
    unchanged: int = 0
    cut: int = 0


# ---------------------------------------------------------------------------
# Pure diff helper (unit-testable without a database dependency)
# ---------------------------------------------------------------------------


def classify_roster_diff(
    current_person_ids: set[str],
    incoming_person_ids: set[str],
) -> tuple[set[str], set[str], set[str]]:
    """Return ``(added, unchanged, cut)`` sets from two person-ID snapshots.

    This is a pure function with no side effects — it only computes which
    NBA Stats person IDs are new, retained, or dropped between two roster
    pulls. The caller is responsible for acting on the results.

    Args:
        current_person_ids: Person IDs currently active on the roster
            (i.e. with a non-CUT participation row in the database).
        incoming_person_ids: Person IDs present in the freshly pulled roster.

    Returns:
        Three disjoint sets: ``(added, unchanged, cut)`` where
        - ``added`` = incoming - current (new, need ANNOUNCED assertion),
        - ``unchanged`` = current ∩ incoming (still present, no new rows),
        - ``cut`` = current - incoming (dropped, need CUT assertion).
    """
    added = incoming_person_ids - current_person_ids
    unchanged = current_person_ids & incoming_person_ids
    cut = current_person_ids - incoming_person_ids
    return added, unchanged, cut


# ---------------------------------------------------------------------------
# Private DB helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.utcnow()


def _display_name(year: int, venue_slug: str) -> str:
    return f"{year} {venue_slug.replace('_', ' ').title()} Summer League"


async def _upsert_roster_competition(
    db: AsyncSession,
    key: CompetitionKey,
) -> SummerLeagueEdition:
    """Get or create a ``SummerLeagueEdition`` row for the given key.

    Args:
        db: Async database session.
        key: Competition key (year, league_id, venue_slug).

    Returns:
        The existing or newly-created ``SummerLeagueEdition`` row.
    """
    result = await db.execute(
        select(SummerLeagueEdition).where(
            SummerLeagueEdition.year == key.year,  # type: ignore[arg-type]
            SummerLeagueEdition.league_id == key.league_id,  # type: ignore[arg-type]
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = SummerLeagueEdition(
            year=key.year,
            league_id=key.league_id,
            venue_slug=key.venue_slug,
            display_name=_display_name(key.year, key.venue_slug),
        )
        db.add(row)
    else:
        row.venue_slug = key.venue_slug
        row.updated_at = _utc_now()
    return row


async def _upsert_roster_team_entry(
    db: AsyncSession,
    competition_id: int,
    nba_stats_team_id: str,
) -> SummerLeagueTeamEntry:
    """Get or create a ``SummerLeagueTeamEntry`` keyed on (competition, team_id).

    The ``raw_team_name`` is seeded from the NBA Stats team ID as a placeholder
    and is enriched by the normalization pipeline once box-score data arrives.

    Args:
        db: Async database session.
        competition_id: PK of the parent competition row.
        nba_stats_team_id: NBA Stats team identifier string.

    Returns:
        The existing or newly-created ``SummerLeagueTeamEntry`` row.
    """
    result = await db.execute(
        select(SummerLeagueTeamEntry).where(
            SummerLeagueTeamEntry.competition_id == competition_id,  # type: ignore[arg-type]
            SummerLeagueTeamEntry.nba_stats_team_id == nba_stats_team_id,  # type: ignore[arg-type]
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = SummerLeagueTeamEntry(
            competition_id=competition_id,
            nba_stats_team_id=nba_stats_team_id,
            raw_team_name=nba_stats_team_id,  # placeholder; enriched by normalization
            team_slug=nba_stats_team_id.lower(),
        )
        db.add(row)
    row.updated_at = _utc_now()
    return row


async def _upsert_roster_source_player(
    db: AsyncSession,
    entry: RosterEntry,
    year: int,
) -> SummerLeagueSourceRecord:
    """Get or create a ``SummerLeagueSourceRecord`` keyed on nba_stats_person_id.

    Mirrors the ``_upsert_source_player`` idiom in normalization.py.

    Args:
        db: Async database session.
        entry: Parsed roster entry from the NBA.com ``__NEXT_DATA__`` blob.
        year: Summer League season year; updates first/last-seen bounds.

    Returns:
        The existing or newly-created ``SummerLeagueSourceRecord`` row.
    """
    result = await db.execute(
        select(SummerLeagueSourceRecord).where(
            SummerLeagueSourceRecord.nba_stats_person_id  # type: ignore[arg-type]
            == entry.nba_stats_person_id
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = SummerLeagueSourceRecord(
            nba_stats_person_id=entry.nba_stats_person_id,
            raw_player_name=entry.raw_player_name,
            normalized_name=_normalized_name_key(entry.raw_player_name),
            first_seen_year=year,
            last_seen_year=year,
            resolution_status=SummerLeagueResolutionStatus.UNRESOLVED,
        )
        db.add(row)
    else:
        row.raw_player_name = entry.raw_player_name
        row.normalized_name = _normalized_name_key(entry.raw_player_name)
        row.first_seen_year = (
            year if row.first_seen_year is None else min(row.first_seen_year, year)
        )
        row.last_seen_year = (
            year if row.last_seen_year is None else max(row.last_seen_year, year)
        )
        row.updated_at = _utc_now()
    return row


async def _load_active_participations(
    db: AsyncSession,
    competition_id: int,
    team_entry_id: int,
) -> dict[int, SummerLeagueParticipation]:
    """Return active (non-CUT) participation rows keyed by source_player_id.

    Args:
        db: Async database session.
        competition_id: PK of the competition.
        team_entry_id: PK of the team entry.

    Returns:
        Mapping from source_player_id (int) to participation row.
    """
    result = await db.execute(
        select(SummerLeagueParticipation).where(
            SummerLeagueParticipation.competition_id == competition_id,  # type: ignore[arg-type]
            SummerLeagueParticipation.team_entry_id == team_entry_id,  # type: ignore[arg-type]
            SummerLeagueParticipation.roster_status  # type: ignore[arg-type]
            != AffiliationStatus.CUT,
        )
    )
    return {row.source_player_id: row for row in result.scalars().all()}


async def _fetch_affiliation(
    db: AsyncSession, affiliation_id: Optional[int]
) -> Optional[PlayerAffiliation]:
    """Return the ``PlayerAffiliation`` with ``affiliation_id``, or ``None``.

    ``None`` is returned when ``affiliation_id`` is ``None`` or no matching row
    exists (which may be a row created earlier in this session or a prior commit).
    """
    if affiliation_id is None:
        return None
    result = await db.execute(
        select(PlayerAffiliation).where(
            PlayerAffiliation.id == affiliation_id  # type: ignore[arg-type]
        )
    )
    return result.scalar_one_or_none()


async def _supersede_affiliation(
    db: AsyncSession,
    prior: Optional[PlayerAffiliation],
    new_affiliation: PlayerAffiliation,
    recorded_at: datetime,
) -> None:
    """Append the superseding assertion and stamp the prior row (append-only).

    Adds and flushes ``new_affiliation`` (populating its ``id``), then stamps
    ``prior``'s ``superseded_at``/``updated_at`` timestamps — identity fields on
    the prior row are never mutated, preserving the append-only contract shared
    by reactivation, cut, and box-score healing. Callers are responsible for
    building ``new_affiliation`` (including ``supersedes_id``) and for any
    participation-bridge updates afterward.
    """
    db.add(new_affiliation)
    await db.flush()  # populate new_affiliation.id
    if prior is not None:
        prior.superseded_at = recorded_at
        prior.updated_at = _utc_now()


async def _announce_player(
    db: AsyncSession,
    competition_id: int,
    team_entry_id: int,
    source_player: SummerLeagueSourceRecord,
    entry: RosterEntry,
    recorded_at: datetime,
) -> SummerLeagueParticipation:
    """Insert one ANNOUNCED assertion and one participation bridge row.

    If the player was previously cut (roster_status == CUT), the existing
    participation is reactivated: a new ANNOUNCED assertion supersedes the
    prior CUT assertion and the stable bridge row is updated in place. This
    preserves the append-only contract while preventing a duplicate
    participation row that would collide on the stint uniqueness constraint.

    Args:
        db: Async database session.
        competition_id: PK of the parent competition.
        team_entry_id: PK of the team entry.
        source_player: The resolved (or newly-created) source-player row.
        entry: Parsed roster entry supplying bio and position fields.
        recorded_at: Timestamp to stamp on the new assertion.

    Returns:
        The newly-created or reactivated ``SummerLeagueParticipation`` row.
    """
    source_player_id: int = source_player.id  # type: ignore[assignment]

    # Check for any existing participation (including CUT) before inserting.
    # A previously-cut player who reappears must reuse the same bridge row to
    # avoid colliding on uq_summer_league_participation_comp_team_source_stint.
    existing_result = await db.execute(
        select(SummerLeagueParticipation)
        .where(
            SummerLeagueParticipation.competition_id == competition_id,  # type: ignore[arg-type]
            SummerLeagueParticipation.team_entry_id == team_entry_id,  # type: ignore[arg-type]
            SummerLeagueParticipation.source_player_id == source_player_id,  # type: ignore[arg-type]
        )
        # Latest stint wins; ``.first()`` (not ``scalar_one_or_none``) so a future
        # multi-stint row never raises MultipleResultsFound here.
        .order_by(SummerLeagueParticipation.stint_no.desc())  # type: ignore[attr-defined]
    )
    existing = existing_result.scalars().first()

    if existing is not None:
        if existing.roster_status == AffiliationStatus.CUT:
            # Reactivation: supersede the CUT assertion with a fresh ANNOUNCED one.
            prior_id: Optional[int] = existing.affiliation_id
            prior_affiliation = await _fetch_affiliation(db, prior_id)

            new_affiliation = PlayerAffiliation(
                player_id=prior_affiliation.player_id if prior_affiliation else None,
                nba_team_id=(
                    prior_affiliation.nba_team_id if prior_affiliation else None
                ),
                affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
                status=AffiliationStatus.ANNOUNCED,
                recorded_at=recorded_at,
                supersedes_id=prior_id,
                source="nba_summer_league_roster",
                source_ref=(
                    f"{entry.league_id}/{entry.team_id}/{entry.nba_stats_person_id}"
                ),
            )
            await _supersede_affiliation(
                db, prior_affiliation, new_affiliation, recorded_at
            )

            # Update the stable bridge (bridge is not an assertion; mutation is OK).
            existing.affiliation_id = new_affiliation.id
            existing.roster_status = AffiliationStatus.ANNOUNCED
            existing.jersey_number = entry.jersey
            existing.roster_position = entry.position
            existing.updated_at = _utc_now()
            await db.flush()
        return existing

    affiliation = PlayerAffiliation(
        player_id=source_player.canonical_player_id,
        nba_team_id=None,  # resolved later (T4 ticket)
        affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
        status=AffiliationStatus.ANNOUNCED,
        recorded_at=recorded_at,
        source="nba_summer_league_roster",
        source_ref=(f"{entry.league_id}/{entry.team_id}/{entry.nba_stats_person_id}"),
    )
    db.add(affiliation)
    await db.flush()  # populate affiliation.id

    participation = SummerLeagueParticipation(
        competition_id=competition_id,
        team_entry_id=team_entry_id,
        source_player_id=source_player_id,
        player_id=source_player.canonical_player_id,
        affiliation_id=affiliation.id,
        stint_no=1,
        roster_status=AffiliationStatus.ANNOUNCED,
        jersey_number=entry.jersey,
        roster_position=entry.position,
    )
    db.add(participation)
    await db.flush()
    return participation


async def _heal_box_score_first_affiliation(
    db: AsyncSession,
    participation: SummerLeagueParticipation,
    entry: RosterEntry,
    recorded_at: datetime,
) -> None:
    """Append an ANNOUNCED assertion for a participation discovered via a box score.

    A player who appears in a box score before ever being named on a roster
    pull is born with a ``CONFIRMED`` ``nba_summer_league_box_score`` assertion
    (see ``normalization._ensure_participation``). Once a later roster snapshot
    lists that same player, the announced-roster assertion history is
    incomplete unless this is recorded too. Mirrors the CUT-reactivation
    supersede pattern in ``_announce_player``: a new ANNOUNCED
    ``nba_summer_league_roster`` assertion is appended with ``supersedes_id``
    pointing at the box-score row, and the box-score row is stamped
    ``superseded_at`` — retained, never deleted.

    Does not touch ``participation.roster_status``: box-score corroboration
    (``CONFIRMED``) is stronger evidence than a bare roster announcement, so
    the roster-status promotion semantics are left untouched (owned
    elsewhere). A no-op if the participation's current affiliation is not
    box-score-sourced, which also makes this idempotent across re-loads (the
    healed affiliation's source is ``nba_summer_league_roster``, so a
    subsequent call finds nothing to heal).

    Args:
        db: Async database session.
        participation: The active participation row to check/heal.
        entry: Parsed roster entry supplying source_ref fields.
        recorded_at: Timestamp to stamp on the new assertion.
    """
    prior_id: Optional[int] = participation.affiliation_id
    prior_affiliation = await _fetch_affiliation(db, prior_id)
    if prior_affiliation is None:
        return
    if prior_affiliation.source != "nba_summer_league_box_score":
        return

    new_affiliation = PlayerAffiliation(
        player_id=prior_affiliation.player_id,
        nba_team_id=prior_affiliation.nba_team_id,
        affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
        status=AffiliationStatus.ANNOUNCED,
        recorded_at=recorded_at,
        supersedes_id=prior_id,
        source="nba_summer_league_roster",
        source_ref=(f"{entry.league_id}/{entry.team_id}/{entry.nba_stats_person_id}"),
    )
    await _supersede_affiliation(db, prior_affiliation, new_affiliation, recorded_at)

    # Update the stable bridge to point at the latest assertion in the chain.
    participation.affiliation_id = new_affiliation.id
    participation.updated_at = _utc_now()


async def _cut_player(
    db: AsyncSession,
    participation: SummerLeagueParticipation,
    recorded_at: datetime,
) -> None:
    """Supersede the current assertion with a CUT assertion; update participation.

    The prior assertion row is retained and only its ``superseded_at`` timestamp
    is set — all identity fields remain untouched per the append-only contract.

    Args:
        db: Async database session.
        participation: The active participation row whose player was dropped.
        recorded_at: Timestamp to stamp on the new CUT assertion.
    """
    prior_id: Optional[int] = participation.affiliation_id

    # Fetch the prior assertion (may be from this session or a prior commit).
    prior_affiliation = await _fetch_affiliation(db, prior_id)

    # Insert the superseding CUT assertion (append-only — never overwrite).
    cut_affiliation = PlayerAffiliation(
        player_id=prior_affiliation.player_id if prior_affiliation else None,
        nba_team_id=prior_affiliation.nba_team_id if prior_affiliation else None,
        affiliation_type=AffiliationType.SUMMER_LEAGUE_ROSTER,
        status=AffiliationStatus.CUT,
        recorded_at=recorded_at,
        supersedes_id=prior_id,
        source="nba_summer_league_roster",
        source_ref=prior_affiliation.source_ref if prior_affiliation else None,
    )
    await _supersede_affiliation(db, prior_affiliation, cut_affiliation, recorded_at)

    # Update the stable bridge (participation is NOT an assertion, mutation is OK).
    participation.affiliation_id = cut_affiliation.id
    participation.roster_status = AffiliationStatus.CUT
    participation.updated_at = _utc_now()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def load_roster_snapshot(
    db: AsyncSession,
    competition: CompetitionKey,
    entries: list[RosterEntry],
    *,
    recorded_at: datetime,
) -> RosterDiffReport:
    """Load an NBA.com roster snapshot into the canonical foundation tables.

    Implements append-only assertion semantics:

    - **New player** → one ANNOUNCED ``PlayerAffiliation`` + one
      ``SummerLeagueParticipation`` bridge row.
    - **Unchanged player** (still on roster) → no new rows (idempotent).
    - **Dropped player** → one CUT ``PlayerAffiliation`` superseding the prior;
      participation ``roster_status`` updated to CUT; prior assertion retained
      with ``superseded_at`` set.

    The caller is responsible for committing the session after this call returns.
    This function only flushes (to generate PKs needed for FK wiring).

    Args:
        db: Async database session (stateless; caller commits).
        competition: Year/league/venue key identifying the competition.
        entries: Parsed roster entries from ``roster_parse.parse_roster``.
        recorded_at: Timestamp to stamp on all new assertions created during
            this call (use a fixed value per run for consistent history).

    Returns:
        ``RosterDiffReport`` with per-team and aggregate diff counts.
    """
    # 1. Upsert the competition row and flush to get its PK.
    competition_row = await _upsert_roster_competition(db, competition)
    await db.flush()
    competition_id: int = competition_row.id  # type: ignore[assignment]

    # 2. Group entries by nba_stats_team_id.
    by_team: dict[str, list[RosterEntry]] = {}
    for entry in entries:
        by_team.setdefault(entry.team_id, []).append(entry)

    # 3. Upsert team entries and flush to get their PKs.
    team_rows: dict[str, SummerLeagueTeamEntry] = {}
    for nba_stats_team_id in by_team:
        team_row = await _upsert_roster_team_entry(
            db, competition_id, nba_stats_team_id
        )
        team_rows[nba_stats_team_id] = team_row
    await db.flush()

    # 3b. Find teams that have active participations but are absent from this
    # snapshot — their players must all be cut (empty-team cut, Bug 1).
    absent_result = await db.execute(
        select(SummerLeagueTeamEntry).where(
            SummerLeagueTeamEntry.competition_id == competition_id,  # type: ignore[arg-type]
            SummerLeagueTeamEntry.id.in_(  # type: ignore[union-attr]
                select(SummerLeagueParticipation.team_entry_id)  # type: ignore[call-overload]
                .where(
                    SummerLeagueParticipation.competition_id == competition_id,  # type: ignore[arg-type]
                    SummerLeagueParticipation.roster_status  # type: ignore[arg-type]
                    != AffiliationStatus.CUT,
                )
                .distinct()
            ),
        )
    )
    absent_teams = [
        row
        for row in absent_result.scalars().all()
        if row.nba_stats_team_id not in by_team
    ]

    # 4. Process each team.
    report = RosterDiffReport()

    for nba_stats_team_id, team_entries in by_team.items():
        team_entry = team_rows[nba_stats_team_id]
        team_entry_id: int = team_entry.id  # type: ignore[assignment]
        team_diff = TeamDiff()

        # 4a. Load existing active (non-CUT) participations for this team.
        active_by_sp_id = await _load_active_participations(
            db, competition_id, team_entry_id
        )

        # 4b. Build reverse map: nba_stats_person_id → participation row.
        current_by_person_id: dict[str, SummerLeagueParticipation] = {}
        if active_by_sp_id:
            sp_ids = list(active_by_sp_id.keys())
            sp_result = await db.execute(
                select(SummerLeagueSourceRecord).where(
                    SummerLeagueSourceRecord.id.in_(sp_ids)  # type: ignore[union-attr]
                )
            )
            for sp in sp_result.scalars().all():
                if sp.id is not None:
                    participation = active_by_sp_id[sp.id]
                    current_by_person_id[sp.nba_stats_person_id] = participation

        # 4c. Classify the roster diff.
        current_person_ids = set(current_by_person_id.keys())
        incoming_person_ids = {e.nba_stats_person_id for e in team_entries}
        added_ids, unchanged_ids, cut_ids = classify_roster_diff(
            current_person_ids, incoming_person_ids
        )

        incoming_by_person_id = {e.nba_stats_person_id: e for e in team_entries}

        # 4d. Handle adds: new ANNOUNCED assertion + new participation.
        for person_id in sorted(added_ids):  # sorted for deterministic order
            entry = incoming_by_person_id[person_id]
            source_player = await _upsert_roster_source_player(
                db, entry, competition.year
            )
            await db.flush()
            await _announce_player(
                db, competition_id, team_entry_id, source_player, entry, recorded_at
            )
            team_diff.added += 1

        # 4e. Handle unchanged: update source-player metadata; refresh denormalized
        # convenience fields on the bridge if jersey/position changed (no new assertion).
        # Also heal box-score-first participations (discovered from a game before
        # being announced) by appending a superseding ANNOUNCED assertion so the
        # announced-roster history is complete.
        for person_id in unchanged_ids:
            entry = incoming_by_person_id[person_id]
            await _upsert_roster_source_player(db, entry, competition.year)
            participation = current_by_person_id[person_id]
            if (
                participation.jersey_number != entry.jersey
                or participation.roster_position != entry.position
            ):
                participation.jersey_number = entry.jersey
                participation.roster_position = entry.position
                participation.updated_at = _utc_now()
            await _heal_box_score_first_affiliation(
                db, participation, entry, recorded_at
            )
            team_diff.unchanged += 1

        # 4f. Handle cuts: new CUT assertion superseding the prior.
        for person_id in cut_ids:
            participation = current_by_person_id[person_id]
            await _cut_player(db, participation, recorded_at)
            team_diff.cut += 1

        report.per_team[nba_stats_team_id] = team_diff
        report.added += team_diff.added
        report.unchanged += team_diff.unchanged
        report.cut += team_diff.cut

    # 5. Cut all active players for teams absent from this snapshot (Bug 1).
    for absent_team in absent_teams:
        absent_team_entry_id: int = absent_team.id  # type: ignore[assignment]
        absent_active = await _load_active_participations(
            db, competition_id, absent_team_entry_id
        )
        if not absent_active:
            continue

        sp_result = await db.execute(
            select(SummerLeagueSourceRecord).where(
                SummerLeagueSourceRecord.id.in_(  # type: ignore[union-attr]
                    list(absent_active.keys())
                )
            )
        )
        absent_by_person_id: dict[str, SummerLeagueParticipation] = {}
        for sp in sp_result.scalars().all():
            if sp.id is not None:
                absent_by_person_id[sp.nba_stats_person_id] = absent_active[sp.id]

        absent_diff = TeamDiff()
        for person_id in absent_by_person_id:
            await _cut_player(db, absent_by_person_id[person_id], recorded_at)
            absent_diff.cut += 1

        report.per_team[absent_team.nba_stats_team_id] = absent_diff
        report.cut += absent_diff.cut

    await db.flush()
    return report
