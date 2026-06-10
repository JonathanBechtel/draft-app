"""Resolve Summer League source players to canonical DraftGuru players."""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeaguePlayerGameLog,
    SummerLeaguePlayerResolutionReview,
    SummerLeagueReviewStatus,
    SummerLeagueResolutionStatus,
    SummerLeagueSourcePlayer,
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
    results: list[SummerLeagueResolutionResult] = field(default_factory=list)


def _collapse_whitespace(value: str) -> str:
    """Collapse repeated whitespace and trim the result."""
    return re.sub(r"\s+", " ", value.strip())


_NAME_SUFFIX_RE = re.compile(
    r"\s+(jr|sr|ii|iii|iv|v)\.?\s*$",
    re.IGNORECASE,
)


def normalize_player_name(name: str) -> str:
    """Return a suffix- and diacritic-insensitive player-name key."""
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    without_suffix = _NAME_SUFFIX_RE.sub("", ascii_only)
    return _collapse_whitespace(without_suffix).lower()


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
    return _result_from_confirmed_link(
        source_player,
        player_id=player_id,
        status=status,
        method=method,
        external_id_created=external_id_created,
        stub_created=stub_created,
        logs_backfilled=logs_backfilled,
    )


async def _find_unique_normalized_display_match(
    db: AsyncSession,
    source_name: str,
) -> int | None:
    """Return a unique player whose normalized display name matches."""
    needle = normalize_player_name(source_name)
    if not needle:
        return None

    result = await db.execute(
        select(PlayerMaster.id, PlayerMaster.display_name)  # type: ignore[call-overload]
    )
    matches: set[int] = set()
    for player_id, display_name in result.all():
        if display_name and normalize_player_name(display_name) == needle:
            matches.add(int(player_id))
            if len(matches) > 1:
                return None
    return next(iter(matches)) if len(matches) == 1 else None


async def _find_unique_normalized_alias_match(
    db: AsyncSession,
    source_name: str,
) -> int | None:
    """Return a unique player whose normalized alias matches."""
    needle = normalize_player_name(source_name)
    if not needle:
        return None

    result = await db.execute(
        select(PlayerAlias.player_id, PlayerAlias.full_name)  # type: ignore[call-overload]
    )
    matches: set[int] = set()
    for player_id, full_name in result.all():
        if normalize_player_name(full_name) == needle:
            matches.add(int(player_id))
            if len(matches) > 1:
                return None
    return next(iter(matches)) if len(matches) == 1 else None


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


async def resolve_source_player(
    db: AsyncSession,
    source_player: SummerLeagueSourcePlayer,
    *,
    create_stub: bool = False,
) -> SummerLeagueResolutionResult:
    """Resolve one Summer League source player through the configured cascade.

    The caller owns transaction scope. Confirmed resolutions update
    ``source_player``, ensure the NBA Stats external ID exists, and backfill
    existing Summer League player game logs.
    """
    if not source_player.nba_stats_person_id:
        raise ValueError("source_player.nba_stats_person_id is required.")

    external_player_id = await _find_external_id_player(
        db,
        source_player.nba_stats_person_id,
    )
    if external_player_id is not None:
        return await _confirm_resolution(
            db,
            source_player,
            player_id=external_player_id,
            status=SummerLeagueResolutionStatus.EXTERNAL_ID,
            method=SummerLeagueResolutionStatus.EXTERNAL_ID.value,
        )

    if source_player.canonical_player_id is not None:
        existing_status = source_player.resolution_status
        if existing_status in {
            SummerLeagueResolutionStatus.UNRESOLVED,
            SummerLeagueResolutionStatus.VECTOR_CANDIDATE,
        }:
            existing_status = SummerLeagueResolutionStatus.MANUAL
        return await _confirm_resolution(
            db,
            source_player,
            player_id=source_player.canonical_player_id,
            status=existing_status,
            method="EXISTING_SOURCE",
            preserve_existing_attribution=True,
        )

    exact_player_id = await _find_unique_normalized_display_match(
        db,
        source_player.raw_player_name,
    )
    if exact_player_id is not None:
        return await _confirm_resolution(
            db,
            source_player,
            player_id=exact_player_id,
            status=SummerLeagueResolutionStatus.EXACT,
            method=SummerLeagueResolutionStatus.EXACT.value,
        )

    alias_player_id = await _find_unique_normalized_alias_match(
        db,
        source_player.raw_player_name,
    )
    if alias_player_id is not None:
        return await _confirm_resolution(
            db,
            source_player,
            player_id=alias_player_id,
            status=SummerLeagueResolutionStatus.ALIAS,
            method=SummerLeagueResolutionStatus.ALIAS.value,
        )

    try:
        candidates = await _collect_candidates(db, source_player)
    except SummerLeagueCandidateSearchError:
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
    if candidates and _has_serious_candidate(candidates):
        now = datetime.utcnow()
        source_player.resolution_status = SummerLeagueResolutionStatus.VECTOR_CANDIDATE
        source_player.resolution_confidence = max(
            candidate.score for candidate in candidates
        )
        source_player.resolution_candidates = _candidate_payloads(candidates)
        source_player.updated_at = now
        db.add(source_player)
        await ensure_pending_resolution_review(db, source_player, candidates)
        return SummerLeagueResolutionResult(
            source_player_id=source_player.id,
            nba_stats_person_id=source_player.nba_stats_person_id,
            raw_player_name=source_player.raw_player_name,
            player_id=None,
            status=SummerLeagueResolutionStatus.VECTOR_CANDIDATE,
            method=SummerLeagueResolutionStatus.VECTOR_CANDIDATE.value,
            confidence=source_player.resolution_confidence,
            candidates=candidates,
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
        max((candidate.score for candidate in candidates), default=0.0)
        if candidates
        else None
    )
    source_player.resolution_candidates = (
        _candidate_payloads(candidates) if candidates else None
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
        candidates=candidates,
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

    stmt = (
        select(SummerLeagueSourcePlayer)
        .join(
            SummerLeaguePlayerGameLog,
            SummerLeaguePlayerGameLog.source_player_id == SummerLeagueSourcePlayer.id,  # type: ignore[arg-type]
        )
        .join(
            SummerLeagueCompetition,
            SummerLeagueCompetition.id == SummerLeaguePlayerGameLog.competition_id,  # type: ignore[arg-type]
        )
        .order_by(SummerLeagueSourcePlayer.id)  # type: ignore[arg-type]
    )
    if year is not None:
        stmt = stmt.where(SummerLeagueCompetition.year == year)  # type: ignore[arg-type]
    if league_id is not None:
        stmt = stmt.where(SummerLeagueCompetition.league_id == league_id)  # type: ignore[arg-type]
    stmt = stmt.distinct(SummerLeagueSourcePlayer.id)  # type: ignore[arg-type]

    result = await db.execute(stmt)
    return list(result.scalars().all())


def _build_report(
    *,
    year: int | None,
    league_id: str | None,
    results: list[SummerLeagueResolutionResult],
) -> SummerLeagueResolutionReport:
    """Build aggregate counters from individual resolution results."""
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
        results=results,
    )


async def resolve_summer_league_players(
    db: AsyncSession,
    *,
    year: int | None = None,
    league_id: str | None = None,
    create_stubs: bool = False,
) -> SummerLeagueResolutionReport:
    """Resolve Summer League source players in a selected batch scope."""
    source_players = await _load_source_players(db, year=year, league_id=league_id)
    results: list[SummerLeagueResolutionResult] = []
    for source_player in source_players:
        results.append(
            await resolve_source_player(
                db,
                source_player,
                create_stub=create_stubs,
            )
        )
    return _build_report(year=year, league_id=league_id, results=results)
