"""Integration tests for the Summer League Desk projection tables (T1-T4).

Schema-roundtrip coverage only — these tables are rebuildable read-model
projections (behavior spec §10); the builders/services that populate them are
out of scope for this ticket.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueTeamEntry,
)
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrade,
    SummerLeagueDeskGrain,
    SummerLeagueDeskPlayerGrade,
    SummerLeagueDeskSlate,
    SummerLeagueDeskStoryline,
    SummerLeagueDeskTriggerType,
)
from tests.integration.conftest import make_player

_N = {"i": 0}


async def _seed_game_context(
    db: AsyncSession,
) -> tuple[SummerLeagueCompetition, SummerLeagueGame]:
    _N["i"] += 1
    idx = _N["i"]
    competition = SummerLeagueCompetition(
        year=2026,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2026 Las Vegas Summer League",
    )
    db.add(competition)
    await db.flush()
    assert competition.id is not None

    team = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id=f"t-desk-{idx}",
        raw_team_name="Test Team",
        team_slug=f"test-team-{idx}",
    )
    db.add(team)
    await db.flush()
    assert team.id is not None

    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id=f"desk-game-{idx}",
        game_date=date(2026, 7, 12),
        home_team_entry_id=team.id,
        status=SummerLeagueGameStatus.SCHEDULED,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None
    return competition, game


@pytest.mark.asyncio
async def test_cohort_baseline_roundtrip(db_session: AsyncSession) -> None:
    """T1 baseline rows persist enums, breakpoints json, and version/cohort uniqueness."""
    baseline = SummerLeagueCohortBaseline(
        baseline_version="2026.1",
        is_active=True,
        cohort_key="slot:1-4",
        cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
        slot_low=1,
        slot_high=4,
        metric="gmsc",
        grain=SummerLeagueDeskGrain.EVENT,
        venue_scope="all",
        season_range="2017-2025",
        min_minutes=40.0,
        n_members=32,
        breakpoints={"50": 12.5, "90": 20.1},
        mean_value=13.4,
        median_value=12.5,
    )
    db_session.add(baseline)
    await db_session.flush()
    await db_session.refresh(baseline)

    assert baseline.id is not None
    assert baseline.cohort_kind == SummerLeagueDeskCohortKind.SLOT_WINDOW
    assert baseline.grain == SummerLeagueDeskGrain.EVENT
    assert baseline.breakpoints == {"50": 12.5, "90": 20.1}

    fetched = await db_session.get(SummerLeagueCohortBaseline, baseline.id)
    assert fetched is not None
    assert fetched.cohort_key == "slot:1-4"


@pytest.mark.asyncio
async def test_cohort_baseline_requires_unique_version_and_cohort_key(
    db_session: AsyncSession,
) -> None:
    """A (baseline_version, cohort_key) pair is unique."""
    kwargs = dict(
        baseline_version="2026.2",
        cohort_key="round:2",
        cohort_kind=SummerLeagueDeskCohortKind.ROUND_BUCKET,
        grain=SummerLeagueDeskGrain.EVENT,
        season_range="2017-2025",
    )
    db_session.add(SummerLeagueCohortBaseline(**kwargs))
    await db_session.flush()

    db_session.add(SummerLeagueCohortBaseline(**kwargs))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_cohort_baseline_active_index_rejects_duplicate_cohort(
    db_session: AsyncSession,
) -> None:
    """Different baseline versions cannot both be active for one cohort."""
    common = dict(
        cohort_key="status:undrafted",
        cohort_kind=SummerLeagueDeskCohortKind.STATUS,
        grain=SummerLeagueDeskGrain.EVENT,
        season_range="2017-2025",
    )
    db_session.add(
        SummerLeagueCohortBaseline(
            baseline_version="2026.1",
            is_active=True,
            **common,
        )
    )
    await db_session.commit()

    db_session.add(
        SummerLeagueCohortBaseline(
            baseline_version="2026.2",
            is_active=True,
            **common,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_player_grade_roundtrip(db_session: AsyncSession) -> None:
    """T2 player grade rows persist facts json and enforce the per-player-event-version key."""
    competition, _game = await _seed_game_context(db_session)
    player = make_player("Desk", "Prospect")
    db_session.add(player)
    await db_session.flush()
    assert player.id is not None

    grade = SummerLeagueDeskPlayerGrade(
        player_id=player.id,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version="2026.1",
        cohort_key="slot:1-4",
        subject_value=18.9,
        pctl=96.0,
        grade=SummerLeagueDeskGrade.HOT,
        n_cohort=32,
        gated=False,
        facts=[{"kind": "percentile", "notability": 0.9}],
    )
    db_session.add(grade)
    await db_session.flush()
    await db_session.refresh(grade)

    assert grade.id is not None
    assert grade.grade == SummerLeagueDeskGrade.HOT
    assert grade.facts == [{"kind": "percentile", "notability": 0.9}]

    duplicate = SummerLeagueDeskPlayerGrade(
        player_id=player.id,
        competition_id=competition.id,  # type: ignore[arg-type]
        baseline_version="2026.1",
        cohort_key="slot:1-4",
        subject_value=10.0,
        pctl=40.0,
        grade=SummerLeagueDeskGrade.MID,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_storyline_roundtrip(db_session: AsyncSession) -> None:
    """T3 storyline rows persist trigger enum, optional second subject, and weighting."""
    competition, game = await _seed_game_context(db_session)
    subject = make_player("Duel", "SubjectOne")
    subject_2 = make_player("Duel", "SubjectTwo")
    db_session.add(subject)
    db_session.add(subject_2)
    await db_session.flush()
    assert subject.id is not None
    assert subject_2.id is not None

    storyline = SummerLeagueDeskStoryline(
        game_date=date(2026, 7, 12),
        competition_id=competition.id,  # type: ignore[arg-type]
        game_id=game.id,  # type: ignore[arg-type]
        trigger_type=SummerLeagueDeskTriggerType.DUEL,
        subject_player_id=subject.id,
        subject_player_id_2=subject_2.id,
        base_weight=90.0,
        magnitude=1.5,
        weight=135.0,
        realized_deviation=None,
    )
    db_session.add(storyline)
    await db_session.flush()
    await db_session.refresh(storyline)

    assert storyline.id is not None
    assert storyline.trigger_type == SummerLeagueDeskTriggerType.DUEL
    assert storyline.subject_player_id_2 == subject_2.id
    assert storyline.realized_deviation is None

    result = await db_session.execute(
        select(SummerLeagueDeskStoryline).where(
            SummerLeagueDeskStoryline.game_date == date(2026, 7, 12),
            SummerLeagueDeskStoryline.competition_id == competition.id,
        )
    )
    rows = result.scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_slate_roundtrip_and_unique_game(db_session: AsyncSession) -> None:
    """T4 slate rows persist facts json and enforce one row per game."""
    competition, game = await _seed_game_context(db_session)

    slate = SummerLeagueDeskSlate(
        game_date=date(2026, 7, 12),
        competition_id=competition.id,  # type: ignore[arg-type]
        game_id=game.id,  # type: ignore[arg-type]
        total_weight=225.0,
        rank=1,
        is_hero=True,
        facts=[{"kind": "leads_field", "notability": 0.95}],
        computed_at=datetime.utcnow(),
    )
    db_session.add(slate)
    await db_session.flush()
    await db_session.refresh(slate)

    assert slate.id is not None
    assert slate.is_hero is True
    assert slate.facts == [{"kind": "leads_field", "notability": 0.95}]

    duplicate = SummerLeagueDeskSlate(
        game_date=date(2026, 7, 12),
        competition_id=competition.id,  # type: ignore[arg-type]
        game_id=game.id,  # type: ignore[arg-type]
        total_weight=1.0,
        rank=2,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()
