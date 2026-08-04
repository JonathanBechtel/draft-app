"""Integration tests for Summer League player-resolution review rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeaguePlayerResolutionReview,
    SummerLeagueReviewStatus,
    SummerLeagueResolutionStatus,
    SummerLeagueSourceRecord,
)
from app.services.player_mention_service import _normalized_name_key
from app.services.sources.summer_league.player_resolution import (
    SummerLeagueResolutionCandidate,
    ensure_pending_resolution_review,
    record_resolution_review_decision,
    resolve_source_player,
)


@dataclass(frozen=True, slots=True)
class FakeCandidate:
    """Candidate object matching fields consumed by the resolution service."""

    player_id: int
    display_name: str | None
    score: float


async def _source_player(
    db_session: AsyncSession,
    *,
    raw_name: str = "Ambiguous Source",
    person_id: str = "1642001",
) -> SummerLeagueSourceRecord:
    """Create an unresolved Summer League source player for review tests."""
    source_player = SummerLeagueSourceRecord(
        nba_stats_person_id=person_id,
        raw_player_name=raw_name,
        normalized_name=_normalized_name_key(raw_name),
        first_seen_year=2024,
        last_seen_year=2024,
        resolution_status=SummerLeagueResolutionStatus.UNRESOLVED,
    )
    db_session.add(source_player)
    await db_session.flush()
    assert source_player.id is not None
    return source_player


async def _review_count(
    db_session: AsyncSession,
    *,
    source_player_id: int,
) -> int:
    """Return the total number of review rows for a source player."""
    count = await db_session.scalar(
        select(func.count())
        .select_from(SummerLeaguePlayerResolutionReview)
        .where(
            SummerLeaguePlayerResolutionReview.source_player_id == source_player_id  # type: ignore[arg-type]
        )
    )
    return int(count or 0)


@pytest.mark.asyncio
async def test_ambiguous_resolution_creates_one_pending_review(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambiguous candidates persist as a single active pending review row."""
    first_candidate = PlayerMaster(display_name="Candidate One")
    second_candidate = PlayerMaster(display_name="Candidate Two")
    replacement_candidate = PlayerMaster(display_name="Candidate Three")
    db_session.add_all([first_candidate, second_candidate, replacement_candidate])
    await db_session.flush()
    assert first_candidate.id is not None
    assert second_candidate.id is not None
    assert replacement_candidate.id is not None
    source_player = await _source_player(db_session)

    search_results = [
        [
            FakeCandidate(first_candidate.id, first_candidate.display_name, 0.74),
            FakeCandidate(second_candidate.id, second_candidate.display_name, 0.69),
        ],
        [
            FakeCandidate(
                replacement_candidate.id,
                replacement_candidate.display_name,
                0.82,
            )
        ],
    ]

    async def fake_search(
        db: AsyncSession,
        query: str,
        k: int = 5,
    ) -> list[FakeCandidate]:
        return search_results.pop(0)

    monkeypatch.setattr(
        "app.services.sources.summer_league.player_resolution.find_candidate_players",
        fake_search,
    )

    first_result = await resolve_source_player(db_session, source_player)
    second_result = await resolve_source_player(db_session, source_player)

    assert first_result.status == SummerLeagueResolutionStatus.VECTOR_CANDIDATE
    assert second_result.status == SummerLeagueResolutionStatus.VECTOR_CANDIDATE
    assert source_player.id is not None
    assert await _review_count(db_session, source_player_id=source_player.id) == 1

    review = (
        await db_session.execute(
            select(SummerLeaguePlayerResolutionReview).where(
                SummerLeaguePlayerResolutionReview.source_player_id == source_player.id  # type: ignore[arg-type]
            )
        )
    ).scalar_one()

    assert review.status == SummerLeagueReviewStatus.PENDING
    assert review.raw_player_name == "Ambiguous Source"
    assert review.nba_stats_person_id == "1642001"
    assert review.selected_player_id is None
    assert review.reviewed_at is None
    assert review.candidate_players == [
        {
            "player_id": replacement_candidate.id,
            "display_name": "Candidate Three",
            "score": 0.82,
            "method": "HYBRID",
        }
    ]


@pytest.mark.asyncio
async def test_review_decision_persists_selected_player_status_note_and_timestamp(
    db_session: AsyncSession,
) -> None:
    """Review decisions can store selected player, lifecycle status, note, and time."""
    selected_player = PlayerMaster(display_name="Selected Canonical")
    db_session.add(selected_player)
    await db_session.flush()
    assert selected_player.id is not None
    source_player = await _source_player(
        db_session,
        raw_name="Review Needed",
        person_id="1642002",
    )
    reviewed_at = datetime(2026, 6, 9, 15, 30)

    review = await ensure_pending_resolution_review(
        db_session,
        source_player,
        [
            SummerLeagueResolutionCandidate(
                player_id=selected_player.id,
                display_name=selected_player.display_name,
                score=0.91,
            )
        ],
    )
    assert review.id is not None

    updated = await record_resolution_review_decision(
        db_session,
        review_id=review.id,
        status=SummerLeagueReviewStatus.APPROVED,
        selected_player_id=selected_player.id,
        review_note="Matches NBA.com profile.",
        reviewed_at=reviewed_at,
    )

    assert updated is not None
    assert updated.status == SummerLeagueReviewStatus.APPROVED
    assert updated.selected_player_id == selected_player.id
    assert updated.review_note == "Matches NBA.com profile."
    assert updated.reviewed_at == reviewed_at

    persisted = await db_session.get(SummerLeaguePlayerResolutionReview, review.id)
    assert persisted is not None
    assert persisted.status == SummerLeagueReviewStatus.APPROVED
    assert persisted.selected_player_id == selected_player.id
    assert persisted.review_note == "Matches NBA.com profile."
    assert persisted.reviewed_at == reviewed_at
