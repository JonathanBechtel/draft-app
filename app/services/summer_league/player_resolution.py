"""Resolve Summer League source players to canonical DraftGuru players."""

# discipline: file-size cross-cutting network boundary; no new resolution policy

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_affiliation import PlayerAffiliation
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueParticipation,
    SummerLeaguePlayerGameLog,
    SummerLeaguePlayerResolutionReview,
    SummerLeagueReviewStatus,
    SummerLeagueResolutionStatus,
    SummerLeagueShotEvent,
    SummerLeagueSourcePlayer,
)
from app.services.player_identity_guard import (
    IdentityVariantIndex,
    IdentityVariantMatches,
    build_variant_identity_index,
    find_variant_identity_matches,
    identity_suffixes_differ,
    normalize_player_identity_name,
    resolve_variant_identity_match,
)
from app.services.player_mention_service import parse_player_name

logger = logging.getLogger(__name__)

NBA_STATS_SYSTEM = "nba_stats"
STUB_BIO_SOURCE = "summer_league_ingest"
SERIOUS_CANDIDATE_SCORE = 0.3
MAX_CANDIDATES = 5
CANDIDATE_SEARCH_FAILED_METHOD = "CANDIDATE_SEARCH_FAILED"


class _SearchCandidate(Protocol):
    """Subset of player-search candidate fields used by this service."""

    player_id: int
    display_name: str | None
    score: float


class SummerLeagueCandidateSearchError(RuntimeError):
    """Raised when candidate search fails during player resolution."""


async def find_candidate_players(
    db: AsyncSession,
    query: str,
    k: int = MAX_CANDIDATES,
) -> list[_SearchCandidate]:
    """Lazy wrapper around hybrid player search for patchable tests."""
    from app.services.player_search_service import (  # noqa: PLC0415
        find_candidate_players as _find_candidate_players,
    )

    return cast(list[_SearchCandidate], await _find_candidate_players(db, query, k=k))


@dataclass(frozen=True, slots=True)
class SummerLeagueResolutionCandidate:
    """Serializable candidate player for review or later manual resolution."""

    player_id: int
    display_name: str | None
    score: float
    method: str = "HYBRID"


@dataclass(frozen=True, slots=True)
class SummerLeagueResolutionResult:
    """Outcome for resolving one Summer League source player."""

    source_player_id: int | None
    nba_stats_person_id: str
    raw_player_name: str
    player_id: int | None
    status: SummerLeagueResolutionStatus
    method: str
    confidence: float | None = None
    candidates: list[SummerLeagueResolutionCandidate] = field(default_factory=list)
    external_id_created: bool = False
    stub_created: bool = False
    logs_backfilled: int = 0
    participations_backfilled: int = 0
    shots_backfilled: int = 0

    @property
    def resolved(self) -> bool:
        """Return whether the source player now has a canonical player link."""
        return self.player_id is not None


@dataclass(frozen=True, slots=True)
class SummerLeagueResolutionReport:
    """Batch resolution summary for a selected Summer League scope."""

    year: int | None
    league_id: str | None
    total_source_players: int
    resolved_source_players: int
    unresolved_source_players: int
    external_id_resolutions: int
    existing_source_resolutions: int
    exact_resolutions: int
    alias_resolutions: int
    candidate_source_players: int
    stubs_created: int
    player_game_logs_backfilled: int
    participation_rows_backfilled: int
    shot_events_backfilled: int = 0
    results: list[SummerLeagueResolutionResult] = field(default_factory=list)


def _collapse_whitespace(value: str) -> str:
    """Collapse repeated whitespace and trim the result."""
    return re.sub(r"\s+", " ", value.strip())


def normalize_player_name(name: str) -> str:
    """Return the shared variant-aware canonical-player identity key."""
    return normalize_player_identity_name(name)


def _candidate_payloads(
    candidates: list[SummerLeagueResolutionCandidate],
) -> list[dict[str, object]]:
    """Convert typed candidates into JSONB-safe dicts."""
    return [
        {
            "player_id": candidate.player_id,
            "display_name": candidate.display_name,
            "score": round(candidate.score, 6),
            "method": candidate.method,
        }
        for candidate in candidates
    ]


def _serialize_search_candidates(
    candidates: list[_SearchCandidate],
) -> list[SummerLeagueResolutionCandidate]:
    """Normalize hybrid search candidates into resolution DTOs."""
    serialized: list[SummerLeagueResolutionCandidate] = []
    for candidate in candidates:
        serialized.append(
            SummerLeagueResolutionCandidate(
                player_id=candidate.player_id,
                display_name=candidate.display_name,
                score=max(0.0, min(1.0, float(candidate.score))),
            )
        )
    return serialized


async def ensure_pending_resolution_review(
    db: AsyncSession,
    source_player: SummerLeagueSourcePlayer,
    candidates: list[SummerLeagueResolutionCandidate],
) -> SummerLeaguePlayerResolutionReview:
    """Create or update the active pending review row for a source player."""
    if source_player.id is None:
        raise ValueError("source_player.id is required before creating review rows.")

    candidate_payloads = _candidate_payloads(candidates) if candidates else None
    result = await db.execute(
        select(SummerLeaguePlayerResolutionReview).where(
            SummerLeaguePlayerResolutionReview.source_player_id == source_player.id,  # type: ignore[arg-type]
            SummerLeaguePlayerResolutionReview.status
            == SummerLeagueReviewStatus.PENDING,  # type: ignore[arg-type]
        )
    )
    review = result.scalar_one_or_none()
    if review is None:
        review = SummerLeaguePlayerResolutionReview(
            source_player_id=source_player.id,
            raw_player_name=source_player.raw_player_name,
            nba_stats_person_id=source_player.nba_stats_person_id,
            candidate_players=candidate_payloads,
            status=SummerLeagueReviewStatus.PENDING,
        )
    else:
        review.raw_player_name = source_player.raw_player_name
        review.nba_stats_person_id = source_player.nba_stats_person_id
        review.candidate_players = candidate_payloads
        review.selected_player_id = None
        review.review_note = None
        review.reviewed_at = None

    db.add(review)
    await db.flush()
    return review


async def record_resolution_review_decision(
    db: AsyncSession,
    *,
    review_id: int,
    status: SummerLeagueReviewStatus,
    selected_player_id: int | None = None,
    review_note: str | None = None,
    reviewed_at: datetime | None = None,
) -> SummerLeaguePlayerResolutionReview | None:
    """Persist a manual review decision for a resolution-review row."""
    review = await db.get(SummerLeaguePlayerResolutionReview, review_id)
    if review is None:
        return None

    review.status = status
    review.selected_player_id = selected_player_id
    review.review_note = review_note
    review.reviewed_at = (
        None
        if status == SummerLeagueReviewStatus.PENDING
        else reviewed_at or datetime.utcnow()
    )
    db.add(review)
    await db.flush()
    return review


def _has_serious_candidate(
    candidates: list[SummerLeagueResolutionCandidate],
) -> bool:
    """Return whether a candidate is strong enough to block stub creation."""
    return any(candidate.score >= SERIOUS_CANDIDATE_SCORE for candidate in candidates)


def _result_from_confirmed_link(
    source_player: SummerLeagueSourcePlayer,
    *,
    player_id: int,
    status: SummerLeagueResolutionStatus,
    method: str,
    external_id_created: bool,
    logs_backfilled: int,
    participations_backfilled: int = 0,
    shots_backfilled: int = 0,
    stub_created: bool = False,
) -> SummerLeagueResolutionResult:
    """Build a standard result for a confirmed resolution."""
    return SummerLeagueResolutionResult(
        source_player_id=source_player.id,
        nba_stats_person_id=source_player.nba_stats_person_id,
        raw_player_name=source_player.raw_player_name,
        player_id=player_id,
        status=status,
        method=method,
        confidence=1.0,
        external_id_created=external_id_created,
        stub_created=stub_created,
        logs_backfilled=logs_backfilled,
        participations_backfilled=participations_backfilled,
        shots_backfilled=shots_backfilled,
    )


async def _find_external_id_player(
    db: AsyncSession,
    nba_stats_person_id: str,
) -> int | None:
    """Return the canonical player linked to an NBA Stats PERSON_ID."""
    result = await db.execute(
        select(PlayerExternalId.player_id).where(  # type: ignore[attr-defined, call-overload]
            PlayerExternalId.system == NBA_STATS_SYSTEM,
            PlayerExternalId.external_id == nba_stats_person_id,
        )
    )
    player_id = result.scalar_one_or_none()
    return int(player_id) if player_id is not None else None


async def _ensure_nba_stats_external_id(
    db: AsyncSession,
    *,
    player_id: int,
    nba_stats_person_id: str,
) -> bool:
    """Ensure a confirmed resolution has a matching NBA Stats external ID."""
    existing_player_id = await _find_external_id_player(db, nba_stats_person_id)
    if existing_player_id == player_id:
        return False
    if existing_player_id is not None:
        raise ValueError(
            "NBA Stats external id "
            f"{nba_stats_person_id!r} is already linked to player_id="
            f"{existing_player_id}, not player_id={player_id}."
        )

    db.add(
        PlayerExternalId(
            player_id=player_id,
            system=NBA_STATS_SYSTEM,
            external_id=nba_stats_person_id,
        )
    )
    await db.flush()
    return True


@dataclass
class ExternalIdBackfillReport:
    """Outcome of seeding nba_stats external ids from resolved source players.

    Attributes:
        seeded: New ``player_external_ids`` rows inserted.
        already_present: Pairs that were already linked (idempotent no-ops).
        conflicts: ``(person_id, existing_player_id, attempted_player_id)``
            tuples where a PERSON_ID is already linked to a different player than
            the resolved source player claims — surfaced for manual review rather
            than crashing the sweep (a resolution inconsistency, not a seed bug).
    """

    seeded: int = 0
    already_present: int = 0
    conflicts: list[tuple[str, int, int]] = field(default_factory=list)


async def backfill_nba_stats_external_ids(
    db: AsyncSession,
) -> ExternalIdBackfillReport:
    """Seed ``player_external_ids(system='nba_stats')`` from resolved SL players.

    Every resolved ``SummerLeagueSourcePlayer`` already carries both a canonical
    ``player_id`` and an NBA Stats ``PERSON_ID``. This promotes that pair into the
    canonical external-id table so that (a) future resolution is deterministic — an
    O(1) PERSON_ID lookup instead of a fuzzy name match — and (b) C1 headshot URLs
    can join ``players_master`` → external id → the NBA CDN. The sweep is
    idempotent: re-running over unchanged data inserts nothing.

    The caller owns the transaction (commit/rollback); this only flushes.

    Args:
        db: Async database session.

    Returns:
        An :class:`ExternalIdBackfillReport` summarizing the sweep.
    """
    # nba_stats_person_id is unique and non-nullable on source players, so the
    # result carries no duplicate PERSON_IDs — no in-loop dedup is needed.
    result = await db.execute(
        select(  # type: ignore[call-overload]
            SummerLeagueSourcePlayer.canonical_player_id,
            SummerLeagueSourcePlayer.nba_stats_person_id,
        ).where(
            SummerLeagueSourcePlayer.canonical_player_id.isnot(None),  # type: ignore[union-attr]
        )
    )
    report = ExternalIdBackfillReport()
    for canonical_player_id, person_id in result.all():
        # canonical_player_id is non-null by the WHERE above; guard only the
        # (data-integrity) empty PERSON_ID case.
        if not person_id:
            continue
        person_key = str(person_id)
        try:
            created = await _ensure_nba_stats_external_id(
                db,
                player_id=int(canonical_player_id),
                nba_stats_person_id=person_key,
            )
        except ValueError:
            existing = await _find_external_id_player(db, person_key)
            report.conflicts.append(
                (
                    person_key,
                    int(existing) if existing is not None else -1,
                    int(canonical_player_id),
                )
            )
            continue
        if created:
            report.seeded += 1
        else:
            report.already_present += 1
    return report


async def _backfill_player_game_logs(
    db: AsyncSession,
    *,
    source_player_id: int | None,
    player_id: int,
) -> int:
    """Set denormalized canonical player IDs on existing player game logs."""
    if source_player_id is None:
        return 0
    result = await db.execute(
        update(SummerLeaguePlayerGameLog)
        .where(SummerLeaguePlayerGameLog.source_player_id == source_player_id)  # type: ignore[arg-type]
        .values(player_id=player_id, updated_at=datetime.utcnow())
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    return int(rowcount) if rowcount is not None else 0


async def _backfill_shot_events(
    db: AsyncSession,
    *,
    source_player_id: int | None,
    player_id: int,
) -> int:
    """Set the denormalized canonical player ID on existing shot events.

    Shot events are normalized before a source player is resolved, so their
    ``player_id`` is left ``NULL`` until resolution. Mirroring
    :func:`_backfill_player_game_logs` keeps per-player shot charts populated.
    """
    if source_player_id is None:
        return 0
    result = await db.execute(
        update(SummerLeagueShotEvent)
        .where(SummerLeagueShotEvent.source_player_id == source_player_id)  # type: ignore[arg-type]
        .values(player_id=player_id)
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    return int(rowcount) if rowcount is not None else 0


async def _backfill_participation_and_affiliation(
    db: AsyncSession,
    *,
    source_player_id: int | None,
    player_id: int,
) -> int:
    """Backfill canonical player_id onto participation and linked affiliation rows.

    Mirrors ``_backfill_player_game_logs`` but targets the roster-foundation
    tables.  Walks the ``supersedes_id`` chain on affiliation rows so that prior
    ANNOUNCED assertions are also backfilled when a CUT row is the current
    pointer on the participation.

    Args:
        db: Async database session.
        source_player_id: PK of the ``SummerLeagueSourcePlayer`` whose rows
            should be backfilled.  Returns 0 immediately when ``None``.
        player_id: Canonical ``players_master.id`` to write into each row.

    Returns:
        Number of ``summer_league_participation`` rows updated.
    """
    if source_player_id is None:
        return 0

    # 1. Bulk-update all participation rows for this source player.
    part_result = cast(
        CursorResult[Any],
        await db.execute(
            update(SummerLeagueParticipation)
            .where(
                SummerLeagueParticipation.source_player_id == source_player_id  # type: ignore[arg-type]
            )
            .values(player_id=player_id, updated_at=datetime.utcnow())
        ),
    )
    rowcount = int(part_result.rowcount) if part_result.rowcount is not None else 0

    # 2. Collect affiliation IDs directly referenced by those participations.
    aff_result = await db.execute(
        select(SummerLeagueParticipation.affiliation_id).where(  # type: ignore[call-overload]
            SummerLeagueParticipation.source_player_id == source_player_id,  # type: ignore[arg-type]
            SummerLeagueParticipation.affiliation_id.isnot(None),  # type: ignore[union-attr]
        )
    )
    affiliation_ids: set[int] = {int(row[0]) for row in aff_result.all()}

    if not affiliation_ids:
        return rowcount

    # 3. Walk the supersedes_id chain to collect all ancestor affiliation IDs
    #    (e.g. the prior ANNOUNCED row when the current pointer is a CUT row).
    frontier = list(affiliation_ids)
    while frontier:
        parent_result = await db.execute(
            select(PlayerAffiliation.supersedes_id).where(  # type: ignore[call-overload]
                PlayerAffiliation.id.in_(frontier),  # type: ignore[union-attr]
                PlayerAffiliation.supersedes_id.isnot(None),  # type: ignore[union-attr]
            )
        )
        # The query already filters ``supersedes_id IS NOT NULL``, so every row
        # here has a non-null parent id; only dedupe against ids already seen.
        new_parent_ids = [
            int(row[0])
            for row in parent_result.all()
            if int(row[0]) not in affiliation_ids
        ]
        affiliation_ids.update(new_parent_ids)
        frontier = new_parent_ids

    # 4. Bulk-update all collected affiliation rows.
    await db.execute(
        update(PlayerAffiliation)
        .where(PlayerAffiliation.id.in_(list(affiliation_ids)))  # type: ignore[union-attr]
        .values(player_id=player_id, updated_at=datetime.utcnow())
    )

    return rowcount


async def _confirm_resolution(
    db: AsyncSession,
    source_player: SummerLeagueSourcePlayer,
    *,
    player_id: int,
    status: SummerLeagueResolutionStatus,
    method: str,
    stub_created: bool = False,
    preserve_existing_attribution: bool = False,
) -> SummerLeagueResolutionResult:
    """Persist a confirmed resolution and backfill dependent game logs."""
    external_id_created = await _ensure_nba_stats_external_id(
        db,
        player_id=player_id,
        nba_stats_person_id=source_player.nba_stats_person_id,
    )
    now = datetime.utcnow()
    source_player.canonical_player_id = player_id
    source_player.resolution_status = status
    source_player.resolution_confidence = 1.0
    source_player.resolution_candidates = None
    if not preserve_existing_attribution:
        source_player.resolved_at = now
        source_player.resolved_by = "system"
    source_player.updated_at = now
    db.add(source_player)
    logs_backfilled = await _backfill_player_game_logs(
        db,
        source_player_id=source_player.id,
        player_id=player_id,
    )
    participations_backfilled = await _backfill_participation_and_affiliation(
        db,
        source_player_id=source_player.id,
        player_id=player_id,
    )
    shots_backfilled = await _backfill_shot_events(
        db,
        source_player_id=source_player.id,
        player_id=player_id,
    )
    return _result_from_confirmed_link(
        source_player,
        player_id=player_id,
        status=status,
        method=method,
        external_id_created=external_id_created,
        stub_created=stub_created,
        logs_backfilled=logs_backfilled,
        participations_backfilled=participations_backfilled,
        shots_backfilled=shots_backfilled,
    )


def _plan_from_variant_matches(
    *,
    source_player_id: int,
    source_player_name: str,
    matches: IdentityVariantMatches,
) -> SummerLeagueResolutionPlan | None:
    """Build a safe resolution plan from variant-normalized identity matches."""
    resolution = resolve_variant_identity_match(source_player_name, matches)
    if resolution.status == "none":
        return None
    if resolution.status == "suffix_mismatch":
        assert resolution.player_id is not None
        return SummerLeagueResolutionPlan(
            source_player_id=source_player_id,
            kind="VECTOR_CANDIDATE",
            candidates=[
                SummerLeagueResolutionCandidate(
                    player_id=resolution.player_id,
                    display_name=resolution.display_name,
                    score=1.0,
                    method="NORMALIZED_SUFFIX_MISMATCH",
                )
            ],
        )
    if resolution.status in {"exact", "alias"}:
        assert resolution.player_id is not None
        kind: SummerLeagueResolutionPlanKind = (
            "EXACT" if resolution.status == "exact" else "ALIAS"
        )
        return SummerLeagueResolutionPlan(
            source_player_id=source_player_id,
            kind=kind,
            player_id=resolution.player_id,
        )
    if resolution.status == "ambiguous":
        candidates = [
            SummerLeagueResolutionCandidate(
                player_id=player_id,
                display_name=matches.display_name_for(player_id),
                score=1.0,
                method="NORMALIZED_VARIANT_COLLISION",
            )
            for player_id in resolution.candidate_ids
        ]
        return SummerLeagueResolutionPlan(
            source_player_id=source_player_id,
            kind="VECTOR_CANDIDATE",
            candidates=candidates,
        )
    return None


async def _find_prepared_variant_matches(
    db: AsyncSession,
    source_name: str,
    identity_index: IdentityVariantIndex | None,
) -> IdentityVariantMatches:
    """Find variant matches, reusing the caller's batch index when available."""
    if identity_index is None:
        return await find_variant_identity_matches(db, source_name)
    return await find_variant_identity_matches(db, source_name, index=identity_index)


async def _create_stub_player(
    db: AsyncSession,
    source_player: SummerLeagueSourcePlayer,
) -> int:
    """Create a minimal canonical player stub for an unmatched source player."""
    display_name = _collapse_whitespace(source_player.raw_player_name)
    parsed = parse_player_name(display_name)
    player = PlayerMaster(
        first_name=parsed.first_name or None,
        middle_name=parsed.middle_name,
        last_name=parsed.last_name,
        suffix=parsed.suffix,
        display_name=display_name,
        is_stub=True,
        bio_source=STUB_BIO_SOURCE,
    )
    db.add(player)
    await db.flush()
    if player.id is None:
        raise RuntimeError("Stub player insert did not populate player.id.")
    return player.id


async def _collect_candidates(
    db: AsyncSession,
    source_player: SummerLeagueSourcePlayer,
) -> list[SummerLeagueResolutionCandidate]:
    """Collect hybrid lexical/vector candidates without auto-resolving them."""
    try:
        search_hits = await find_candidate_players(
            db,
            source_player.raw_player_name,
            k=MAX_CANDIDATES,
        )
    except Exception:
        logger.exception(
            "summer_league.player_resolution.candidate_search_failed "
            "source_player_id=%s nba_stats_person_id=%s",
            source_player.id,
            source_player.nba_stats_person_id,
        )
        raise SummerLeagueCandidateSearchError(
            "Candidate search failed for Summer League source player "
            f"{source_player.nba_stats_person_id}."
        )
    return _serialize_search_candidates(search_hits)


SummerLeagueResolutionPlanKind = Literal[
    "EXTERNAL_ID",
    "EXISTING_SOURCE",
    "EXACT",
    "ALIAS",
    "VECTOR_CANDIDATE",
    "UNRESOLVED",
    "CANDIDATE_SEARCH_FAILED",
]

# Plan kinds that confirm a match to an existing canonical player through a
# simple, method-name-matches-status lookup (external id / exact / alias).
# ``EXISTING_SOURCE`` is deliberately excluded: it preserves a caller-supplied
# ``existing_status`` and a different ``method`` string, so it keeps its own
# branch in :func:`apply_source_player_resolution_plan`.
_CONFIRMED_MATCH_STATUSES: dict[
    SummerLeagueResolutionPlanKind, SummerLeagueResolutionStatus
] = {
    "EXTERNAL_ID": SummerLeagueResolutionStatus.EXTERNAL_ID,
    "EXACT": SummerLeagueResolutionStatus.EXACT,
    "ALIAS": SummerLeagueResolutionStatus.ALIAS,
}


@dataclass(frozen=True, slots=True)
class SummerLeagueResolutionPlan:
    """A precomputed, read-only resolution decision for one source player.

    Produced by :func:`prepare_source_player_resolution`, which only reads
    from the database and (for the ``candidates`` case) calls the Gemini
    candidate-search API -- it performs no writes, so it is safe to run with
    no transaction or writer-lock held. :func:`apply_source_player_resolution_plan`
    consumes the plan and performs the actual writes inside a short,
    lock-guarded transaction.

    Attributes:
        source_player_id: The ``SummerLeagueSourcePlayer.id`` this plan is for.
        kind: Which resolution branch was decided (see
            :data:`SummerLeagueResolutionPlanKind`).
        player_id: The canonical player to link, for the confirmed-match kinds.
        existing_status: The resolution status to preserve for
            ``EXISTING_SOURCE`` (see :func:`prepare_source_player_resolution`).
        candidates: Hybrid search candidates surfaced for
            ``VECTOR_CANDIDATE``/``UNRESOLVED``.
    """

    source_player_id: int
    kind: SummerLeagueResolutionPlanKind
    player_id: int | None = None
    existing_status: SummerLeagueResolutionStatus | None = None
    candidates: list[SummerLeagueResolutionCandidate] = field(default_factory=list)


async def prepare_source_player_resolution(
    db: AsyncSession,
    source_player: SummerLeagueSourcePlayer,
    *,
    before_candidate_search: Callable[[], Awaitable[None]] | None = None,
    identity_index: IdentityVariantIndex | None = None,
) -> SummerLeagueResolutionPlan:
    """Run the read-only resolution cascade for one source player.

    Performs no database writes. The only network call in this cascade --
    hybrid candidate search, which depends on the Gemini embedding API --
    happens here, so callers must not hold a database transaction or the
    Summer League advisory writer lock while awaiting this function.

    Args:
        db: Async database session.
        source_player: The source player to evaluate.
        before_candidate_search: Boundary closing the read transaction before search.
        identity_index: Optional per-run display/alias index to reuse across rows.

    Returns:
        A :class:`SummerLeagueResolutionPlan` describing what
        :func:`apply_source_player_resolution_plan` should persist.

    Raises:
        ValueError: If ``source_player.id`` or ``nba_stats_person_id`` is missing.
    """
    if source_player.id is None or not source_player.nba_stats_person_id:
        raise ValueError("source_player.id and nba_stats_person_id are required.")

    external_player_id = await _find_external_id_player(
        db,
        source_player.nba_stats_person_id,
    )
    if external_player_id is not None:
        return SummerLeagueResolutionPlan(
            source_player_id=source_player.id,
            kind="EXTERNAL_ID",
            player_id=external_player_id,
        )

    if source_player.canonical_player_id is not None:
        existing_status = source_player.resolution_status
        if existing_status in {
            SummerLeagueResolutionStatus.UNRESOLVED,
            SummerLeagueResolutionStatus.VECTOR_CANDIDATE,
        }:
            existing_status = SummerLeagueResolutionStatus.MANUAL
        return SummerLeagueResolutionPlan(
            source_player_id=source_player.id,
            kind="EXISTING_SOURCE",
            player_id=source_player.canonical_player_id,
            existing_status=existing_status,
        )

    normalized_plan = _plan_from_variant_matches(
        source_player_id=source_player.id,
        source_player_name=source_player.raw_player_name,
        matches=await _find_prepared_variant_matches(
            db,
            source_player.raw_player_name,
            identity_index,
        ),
    )
    if normalized_plan is not None:
        return normalized_plan

    if before_candidate_search is not None:
        await before_candidate_search()
    try:
        if before_candidate_search is None:
            candidates = await _collect_candidates(db, source_player)
        else:
            from app.services.player_search_service import (  # noqa: PLC0415
                candidate_search_boundary,
            )

            with candidate_search_boundary(before_candidate_search):
                candidates = await _collect_candidates(db, source_player)
    except SummerLeagueCandidateSearchError:
        return SummerLeagueResolutionPlan(
            source_player_id=source_player.id,
            kind="CANDIDATE_SEARCH_FAILED",
        )

    if candidates and _has_serious_candidate(candidates):
        return SummerLeagueResolutionPlan(
            source_player_id=source_player.id,
            kind="VECTOR_CANDIDATE",
            candidates=candidates,
        )

    return SummerLeagueResolutionPlan(
        source_player_id=source_player.id,
        kind="UNRESOLVED",
        candidates=candidates,
    )


async def apply_source_player_resolution_plan(
    db: AsyncSession,
    source_player: SummerLeagueSourcePlayer,
    plan: SummerLeagueResolutionPlan,
    *,
    create_stub: bool = False,
    recheck_variant_before_stub: bool = True,
) -> SummerLeagueResolutionResult:
    """Persist the outcome of a previously prepared resolution plan.

    Performs only database writes -- no network calls -- so this is safe to
    run inside a short transaction while holding the Summer League advisory
    writer lock.

    Args:
        db: Async database session.
        source_player: The same source player ``plan`` was prepared for.
        plan: The decision from :func:`prepare_source_player_resolution`.
        create_stub: Whether an unmatched, no-serious-candidate player should
            get a new canonical stub player created for it.
        recheck_variant_before_stub: Whether to run the final identity lookup
            here. Lock-bounded callers may revalidate immediately beforehand.

    Returns:
        The persisted :class:`SummerLeagueResolutionResult`.

    Raises:
        ValueError: If ``plan`` was not prepared for this ``source_player``.
    """
    if source_player.id != plan.source_player_id:
        raise ValueError("plan.source_player_id does not match source_player.id.")

    if plan.kind in _CONFIRMED_MATCH_STATUSES:
        assert plan.player_id is not None
        status = _CONFIRMED_MATCH_STATUSES[plan.kind]
        return await _confirm_resolution(
            db,
            source_player,
            player_id=plan.player_id,
            status=status,
            method=status.value,
        )

    if plan.kind == "EXISTING_SOURCE":
        assert plan.player_id is not None
        assert plan.existing_status is not None
        return await _confirm_resolution(
            db,
            source_player,
            player_id=plan.player_id,
            status=plan.existing_status,
            method="EXISTING_SOURCE",
            preserve_existing_attribution=True,
        )

    if plan.kind == "CANDIDATE_SEARCH_FAILED":
        now = datetime.utcnow()
        source_player.resolution_status = SummerLeagueResolutionStatus.UNRESOLVED
        source_player.resolution_confidence = None
        source_player.resolution_candidates = None
        source_player.updated_at = now
        db.add(source_player)
        return SummerLeagueResolutionResult(
            source_player_id=source_player.id,
            nba_stats_person_id=source_player.nba_stats_person_id,
            raw_player_name=source_player.raw_player_name,
            player_id=None,
            status=SummerLeagueResolutionStatus.UNRESOLVED,
            method=CANDIDATE_SEARCH_FAILED_METHOD,
        )

    if plan.kind == "VECTOR_CANDIDATE":
        now = datetime.utcnow()
        source_player.resolution_status = SummerLeagueResolutionStatus.VECTOR_CANDIDATE
        source_player.resolution_confidence = max(
            candidate.score for candidate in plan.candidates
        )
        source_player.resolution_candidates = _candidate_payloads(plan.candidates)
        source_player.updated_at = now
        db.add(source_player)
        await ensure_pending_resolution_review(db, source_player, plan.candidates)
        return SummerLeagueResolutionResult(
            source_player_id=source_player.id,
            nba_stats_person_id=source_player.nba_stats_person_id,
            raw_player_name=source_player.raw_player_name,
            player_id=None,
            status=SummerLeagueResolutionStatus.VECTOR_CANDIDATE,
            method=SummerLeagueResolutionStatus.VECTOR_CANDIDATE.value,
            confidence=source_player.resolution_confidence,
            candidates=plan.candidates,
        )

    # plan.kind == "UNRESOLVED"
    assert plan.kind == "UNRESOLVED"
    if create_stub and recheck_variant_before_stub:
        late_normalized_plan = _plan_from_variant_matches(
            source_player_id=plan.source_player_id,
            source_player_name=source_player.raw_player_name,
            matches=await find_variant_identity_matches(
                db,
                source_player.raw_player_name,
            ),
        )
        if late_normalized_plan is not None:
            return await apply_source_player_resolution_plan(
                db,
                source_player,
                late_normalized_plan,
                create_stub=False,
                recheck_variant_before_stub=False,
            )
    if create_stub:
        stub_player_id = await _create_stub_player(db, source_player)
        return await _confirm_resolution(
            db,
            source_player,
            player_id=stub_player_id,
            status=SummerLeagueResolutionStatus.STUB,
            method=SummerLeagueResolutionStatus.STUB.value,
            stub_created=True,
        )

    now = datetime.utcnow()
    source_player.resolution_status = SummerLeagueResolutionStatus.UNRESOLVED
    source_player.resolution_confidence = (
        max((candidate.score for candidate in plan.candidates), default=0.0)
        if plan.candidates
        else None
    )
    source_player.resolution_candidates = (
        _candidate_payloads(plan.candidates) if plan.candidates else None
    )
    source_player.updated_at = now
    db.add(source_player)
    return SummerLeagueResolutionResult(
        source_player_id=source_player.id,
        nba_stats_person_id=source_player.nba_stats_person_id,
        raw_player_name=source_player.raw_player_name,
        player_id=None,
        status=SummerLeagueResolutionStatus.UNRESOLVED,
        method=SummerLeagueResolutionStatus.UNRESOLVED.value,
        confidence=source_player.resolution_confidence,
        candidates=plan.candidates,
    )


async def revalidate_source_player_resolution_plan(
    db: AsyncSession,
    source_player: SummerLeagueSourcePlayer,
    plan: SummerLeagueResolutionPlan,
) -> SummerLeagueResolutionPlan:
    """Recheck a prospective stub before entering a writer-lock transaction."""
    if plan.kind != "UNRESOLVED":
        return plan
    return (
        _plan_from_variant_matches(
            source_player_id=plan.source_player_id,
            source_player_name=source_player.raw_player_name,
            matches=await find_variant_identity_matches(
                db,
                source_player.raw_player_name,
            ),
        )
        or plan
    )


async def resolve_source_player(
    db: AsyncSession,
    source_player: SummerLeagueSourcePlayer,
    *,
    create_stub: bool = False,
    before_candidate_search: Callable[[], Awaitable[None]] | None = None,
    identity_index: IdentityVariantIndex | None = None,
) -> SummerLeagueResolutionResult:
    """Resolve one Summer League source player through the configured cascade.

    Convenience wrapper combining :func:`prepare_source_player_resolution` and
    :func:`apply_source_player_resolution_plan` in one call. The caller owns
    transaction scope. Prefer the split functions directly when the caller
    must not hold a database transaction/writer lock across the candidate
    search's Gemini call (see ``summer_league_ingest_runner``'s batched
    resolution phase).
    """
    plan = await prepare_source_player_resolution(
        db,
        source_player,
        before_candidate_search=before_candidate_search,
        identity_index=identity_index,
    )
    return await apply_source_player_resolution_plan(
        db, source_player, plan, create_stub=create_stub
    )


async def _load_source_players(
    db: AsyncSession,
    *,
    year: int | None,
    league_id: str | None,
) -> list[SummerLeagueSourcePlayer]:
    """Load source players in the requested batch scope."""
    if year is None and league_id is None:
        result = await db.execute(
            select(SummerLeagueSourcePlayer)
            .where(
                SummerLeagueSourcePlayer.resolution_status.in_(  # type: ignore[attr-defined]
                    [
                        SummerLeagueResolutionStatus.UNRESOLVED,
                        SummerLeagueResolutionStatus.VECTOR_CANDIDATE,
                    ]
                )
            )
            .order_by(SummerLeagueSourcePlayer.id)  # type: ignore[arg-type]
        )
        return list(result.scalars().all())

    # Build shared competition filter clauses (applied to both subqueries below).
    comp_filters = []
    if year is not None:
        comp_filters.append(SummerLeagueCompetition.year == year)  # type: ignore[arg-type]
    if league_id is not None:
        comp_filters.append(SummerLeagueCompetition.league_id == league_id)  # type: ignore[arg-type]

    # Source players reachable via game logs (pre-2026 stats pipeline).
    gamelog_subq = (
        select(SummerLeaguePlayerGameLog.source_player_id)  # type: ignore[call-overload]
        .join(
            SummerLeagueCompetition,
            SummerLeagueCompetition.id == SummerLeaguePlayerGameLog.competition_id,  # type: ignore[arg-type]
        )
        .where(*comp_filters)
    )

    # Source players reachable via participation rows (roster-loaded, no logs yet).
    participation_subq = (
        select(SummerLeagueParticipation.source_player_id)  # type: ignore[call-overload]
        .join(
            SummerLeagueCompetition,
            SummerLeagueCompetition.id == SummerLeagueParticipation.competition_id,  # type: ignore[arg-type]
        )
        .where(*comp_filters)
    )

    stmt = (
        select(SummerLeagueSourcePlayer)
        .where(
            or_(
                SummerLeagueSourcePlayer.id.in_(gamelog_subq),  # type: ignore[union-attr]
                SummerLeagueSourcePlayer.id.in_(participation_subq),  # type: ignore[union-attr]
            )
        )
        .order_by(SummerLeagueSourcePlayer.id)  # type: ignore[arg-type]
        .distinct()
    )

    result = await db.execute(stmt)
    return list(result.scalars().all())


def build_resolution_report(
    *,
    year: int | None,
    league_id: str | None,
    results: list[SummerLeagueResolutionResult],
) -> SummerLeagueResolutionReport:
    """Build aggregate counters from individual resolution results.

    Public so callers that apply resolution plans in their own batches (e.g.
    ``summer_league_ingest_runner``'s lock-bounded resolution phase) can build
    the same aggregate report shape as :func:`resolve_summer_league_players`
    from their own accumulated results.
    """
    return SummerLeagueResolutionReport(
        year=year,
        league_id=league_id,
        total_source_players=len(results),
        resolved_source_players=sum(1 for result in results if result.resolved),
        unresolved_source_players=sum(1 for result in results if not result.resolved),
        external_id_resolutions=sum(
            1 for result in results if result.method == "EXTERNAL_ID"
        ),
        existing_source_resolutions=sum(
            1 for result in results if result.method == "EXISTING_SOURCE"
        ),
        exact_resolutions=sum(1 for result in results if result.method == "EXACT"),
        alias_resolutions=sum(1 for result in results if result.method == "ALIAS"),
        candidate_source_players=sum(1 for result in results if result.candidates),
        stubs_created=sum(1 for result in results if result.stub_created),
        player_game_logs_backfilled=sum(result.logs_backfilled for result in results),
        participation_rows_backfilled=sum(
            result.participations_backfilled for result in results
        ),
        shot_events_backfilled=sum(result.shots_backfilled for result in results),
        results=results,
    )


async def resolve_summer_league_players(
    db: AsyncSession,
    *,
    year: int | None = None,
    league_id: str | None = None,
    create_stubs: bool = False,
    before_candidate_search: Callable[[], Awaitable[None]] | None = None,
) -> SummerLeagueResolutionReport:
    """Resolve Summer League source players in a selected batch scope.

    Convenience wrapper for callers that run entirely inside one
    caller-owned transaction (e.g. manual/admin scripts not subject to the
    Summer League writer-lock lifetime budget). Callers that must not hold a
    transaction/writer lock across candidate search's Gemini call should use
    :func:`prepare_summer_league_player_resolutions` and
    :func:`apply_source_player_resolution_plan` directly instead (see
    ``summer_league_ingest_runner``'s batched resolution phase).
    """
    source_players = await _load_source_players(db, year=year, league_id=league_id)
    results: list[SummerLeagueResolutionResult] = []
    for source_player in source_players:
        if before_candidate_search is None:
            result = await resolve_source_player(
                db,
                source_player,
                create_stub=create_stubs,
            )
        else:
            result = await resolve_source_player(
                db,
                source_player,
                create_stub=create_stubs,
                before_candidate_search=before_candidate_search,
            )
        results.append(result)
    return build_resolution_report(year=year, league_id=league_id, results=results)


async def prepare_summer_league_player_resolutions(
    db: AsyncSession,
    *,
    year: int | None = None,
    league_id: str | None = None,
    before_candidate_search: Callable[[], Awaitable[None]] | None = None,
) -> list[tuple[SummerLeagueSourcePlayer, SummerLeagueResolutionPlan]]:
    """Load the selected batch scope and prepare a plan for each source player.

    Performs no database writes -- safe to call with no writer-lock
    transaction held. This is where every candidate-search/Gemini call for
    the batch happens. Callers that need a bounded writer-lock lifetime
    should chunk the returned pairs and pass each one to
    :func:`apply_source_player_resolution_plan` inside its own short,
    lock-guarded transaction.

    Args:
        db: Async database session with no open transaction.
        year: Optional Summer League season year to scope the batch.
        league_id: Optional NBA Stats LeagueID to scope the batch.
        before_candidate_search: Boundary invoked before each candidate search.

    Returns:
        ``(source_player, plan)`` pairs in the same order
        :func:`resolve_summer_league_players` would process them.
    """
    source_players = await _load_source_players(db, year=year, league_id=league_id)
    identity_index = await build_variant_identity_index(db)
    pairs: list[tuple[SummerLeagueSourcePlayer, SummerLeagueResolutionPlan]] = []
    for source_player in source_players:
        plan = await prepare_source_player_resolution(
            db,
            source_player,
            before_candidate_search=before_candidate_search,
            identity_index=identity_index,
        )
        pairs.append((source_player, plan))
    return pairs


def _suffixes_differ(source_name: str, candidate_name: str | None) -> bool:
    """Return whether two names carry different recognized suffixes."""
    return identity_suffixes_differ(source_name, candidate_name)
