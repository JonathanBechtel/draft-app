"""Integration tests for the Summer League Desk percentile + grade service (#503).

Seeds a T1 baseline (via #502's ``build_baselines``) plus subject
``SummerLeaguePlayerSeason`` rows, runs ``grade_player_event``, and asserts
the persisted T2 (`summer_league_desk_player_grades`) row end to end,
including the adaptive gate-ladder suppression on thin/1-game samples and a
thin-cohort baseline.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import SummerLeagueEdition
from app.schemas.summer_league_desk import (
    SummerLeagueDeskGrade,
    SummerLeagueDeskPlayerGrade,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league.cohort_baselines import (
    build_baselines,
    compute_breakpoints,
)
from app.services.summer_league.desk_grades import GradeRow, grade_player_event
from app.services.summer_league_leaders_service import TARGET_BOARD_ROWS

pytestmark = pytest.mark.asyncio


async def _seed_competition(
    db: AsyncSession, *, year: int, venue_slug: str = "las_vegas", league_id: str = "13"
) -> SummerLeagueEdition:
    comp = SummerLeagueEdition(
        year=year,
        league_id=league_id,
        venue_slug=venue_slug,
        display_name=f"{year} {venue_slug}",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 10),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def _seed_player(
    db: AsyncSession,
    *,
    name: str,
    draft_round: int | None,
    draft_pick: int | None,
) -> PlayerMaster:
    player = PlayerMaster(
        first_name=name,
        last_name="Test",
        display_name=f"{name} Test",
        draft_year=2020,
        draft_round=draft_round,
        draft_pick=draft_pick,
        is_stub=False,
    )
    db.add(player)
    await db.flush()
    assert player.id is not None
    return player


async def _seed_season(
    db: AsyncSession,
    *,
    competition: SummerLeagueEdition,
    player: PlayerMaster,
    year: int,
    gmsc: float,
    minutes: float,
    gp: int,
) -> SummerLeaguePlayerSeason:
    assert competition.id is not None
    assert player.id is not None
    season = SummerLeaguePlayerSeason(
        competition_id=competition.id,
        player_id=player.id,
        year=year,
        venue_slug=competition.venue_slug,
        is_current=True,
        gp=gp,
        minutes=minutes,
        gmsc=gmsc,
    )
    db.add(season)
    await db.flush()
    return season


async def _seed_lottery_cohort_history(
    db: AsyncSession, *, values: list[float], start_year: int
) -> None:
    """Seed ``len(values)`` distinct pick-#1 players, one per year from start_year."""
    for i, gmsc in enumerate(values):
        year = start_year + i
        comp = await _seed_competition(db, year=year)
        player = await _seed_player(
            db, name=f"Hist{i}", draft_round=1, draft_pick=1
        )
        await _seed_season(
            db, competition=comp, player=player, year=year, gmsc=gmsc, minutes=100.0, gp=5
        )


async def test_grade_player_event_confident_sample_matches_percentile_inversion(
    db_session: AsyncSession,
) -> None:
    """A healthy sample vs a healthy (n=10) cohort yields an ungated, correct pctl."""
    history = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    await _seed_lottery_cohort_history(db_session, values=history, start_year=2014)

    version = await build_baselines(
        db_session, season_range="2014-2023", min_minutes=40.0
    )
    await db_session.flush()

    subject_comp = await _seed_competition(db_session, year=2024)
    subject = await _seed_player(
        db_session, name="Subject", draft_round=1, draft_pick=1
    )
    await _seed_season(
        db_session,
        competition=subject_comp,
        player=subject,
        year=2024,
        gmsc=75.0,
        minutes=150.0,
        gp=5,
    )

    assert subject.id is not None
    assert subject_comp.id is not None
    row = await grade_player_event(
        db_session,
        player_id=subject.id,
        competition_id=subject_comp.id,
        baseline_version=version,
    )

    expected_pctl_grid = compute_breakpoints(history)
    # Locate the expected percentile the same way percentile_of_value does,
    # independently re-derived here so the assertion isn't circular.
    points = sorted((int(p), v) for p, v in expected_pctl_grid.items())
    expected_pctl = None
    for (p_lo, v_lo), (p_hi, v_hi) in zip(points, points[1:]):
        if v_lo <= 75.0 <= v_hi:
            frac = (75.0 - v_lo) / (v_hi - v_lo)
            expected_pctl = round(p_lo + frac * (p_hi - p_lo), 2)
            break
    assert expected_pctl is not None

    assert isinstance(row, GradeRow)
    assert row.cohort_key == "slot:1-4"
    assert row.subject_value == 75.0
    assert row.pctl == expected_pctl
    assert row.n_cohort == 10
    assert row.gated is False
    assert row.grade in (SummerLeagueDeskGrade.WARM, SummerLeagueDeskGrade.HOT)

    persisted = (
        await db_session.execute(
            select(SummerLeagueDeskPlayerGrade).where(
                SummerLeagueDeskPlayerGrade.player_id == subject.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.competition_id == subject_comp.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.baseline_version == version,  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert persisted.subject_value == 75.0
    assert persisted.cohort_key == "slot:1-4"
    assert persisted.gated is False
    assert persisted.grade == row.grade


async def test_grade_player_event_blends_across_venues_same_year(
    db_session: AsyncSession,
) -> None:
    """Subject value blends every venue the player played that year, games-weighted."""
    history = [10.0] * TARGET_BOARD_ROWS
    await _seed_lottery_cohort_history(db_session, values=history, start_year=2010)
    version = await build_baselines(
        db_session, season_range="2010-2019", min_minutes=0.0
    )
    await db_session.flush()

    subject = await _seed_player(
        db_session, name="MultiVenue", draft_round=1, draft_pick=1
    )
    vegas = await _seed_competition(db_session, year=2024, venue_slug="las_vegas")
    sac = await _seed_competition(
        db_session, year=2024, venue_slug="sacramento", league_id="14"
    )
    await _seed_season(
        db_session, competition=vegas, player=subject, year=2024, gmsc=10.0, minutes=50.0, gp=2
    )
    await _seed_season(
        db_session, competition=sac, player=subject, year=2024, gmsc=20.0, minutes=50.0, gp=2
    )

    assert subject.id is not None
    assert vegas.id is not None
    row = await grade_player_event(
        db_session, player_id=subject.id, competition_id=vegas.id, baseline_version=version
    )
    # games-weighted mean: (10*2 + 20*2) / 4 == 15.0
    assert row.subject_value == 15.0
    assert row.gated is False  # gp=4, minutes=100 clears the standard gate


async def test_grade_player_event_one_game_sample_is_gated(
    db_session: AsyncSession,
) -> None:
    """A 1-game subject sample is gated even against a healthy (n=10) cohort."""
    history = [float(v) for v in range(10, 101, 10)]
    await _seed_lottery_cohort_history(db_session, values=history, start_year=2014)
    version = await build_baselines(
        db_session, season_range="2014-2023", min_minutes=40.0
    )
    await db_session.flush()

    subject_comp = await _seed_competition(db_session, year=2024)
    subject = await _seed_player(db_session, name="OneGame", draft_round=1, draft_pick=1)
    await _seed_season(
        db_session,
        competition=subject_comp,
        player=subject,
        year=2024,
        gmsc=55.0,
        minutes=15.0,
        gp=1,
    )

    assert subject.id is not None
    assert subject_comp.id is not None
    row = await grade_player_event(
        db_session,
        player_id=subject.id,
        competition_id=subject_comp.id,
        baseline_version=version,
    )
    assert row.n_cohort == 10
    assert row.gated is True


async def test_grade_player_event_thin_cohort_gates_even_confident_subject(
    db_session: AsyncSession,
) -> None:
    """A cohort baseline with < TARGET_BOARD_ROWS members gates a confident subject."""
    # Only 3 historical round:1_late players -- well under TARGET_BOARD_ROWS.
    for i, gmsc in enumerate([12.0, 14.0, 16.0]):
        year = 2018 + i
        comp = await _seed_competition(db_session, year=year)
        player = await _seed_player(
            db_session, name=f"Late{i}", draft_round=1, draft_pick=20
        )
        await _seed_season(
            db_session, competition=comp, player=player, year=year, gmsc=gmsc, minutes=100.0, gp=5
        )

    version = await build_baselines(
        db_session, season_range="2018-2020", min_minutes=40.0
    )
    await db_session.flush()

    subject_comp = await _seed_competition(db_session, year=2024)
    subject = await _seed_player(
        db_session, name="ConfidentThinCohort", draft_round=1, draft_pick=20
    )
    await _seed_season(
        db_session,
        competition=subject_comp,
        player=subject,
        year=2024,
        gmsc=13.0,
        minutes=120.0,
        gp=5,
    )

    assert subject.id is not None
    assert subject_comp.id is not None
    row = await grade_player_event(
        db_session,
        player_id=subject.id,
        competition_id=subject_comp.id,
        baseline_version=version,
    )
    assert row.cohort_key == "round:1_late"
    assert row.n_cohort == 3
    assert row.gated is True  # subject sample is fine; the cohort itself is thin


async def test_grade_player_event_upsert_is_idempotent_on_rerun(
    db_session: AsyncSession,
) -> None:
    """Re-grading the same (player, competition, baseline_version) updates in place."""
    history = [float(v) for v in range(10, 101, 10)]
    await _seed_lottery_cohort_history(db_session, values=history, start_year=2014)
    version = await build_baselines(
        db_session, season_range="2014-2023", min_minutes=40.0
    )
    await db_session.flush()

    subject_comp = await _seed_competition(db_session, year=2024)
    subject = await _seed_player(db_session, name="Rerun", draft_round=1, draft_pick=1)
    await _seed_season(
        db_session,
        competition=subject_comp,
        player=subject,
        year=2024,
        gmsc=50.0,
        minutes=150.0,
        gp=5,
    )

    assert subject.id is not None
    assert subject_comp.id is not None
    first = await grade_player_event(
        db_session, player_id=subject.id, competition_id=subject_comp.id, baseline_version=version
    )
    second = await grade_player_event(
        db_session, player_id=subject.id, competition_id=subject_comp.id, baseline_version=version
    )
    assert first.subject_value == second.subject_value

    rows = (
        await db_session.execute(
            select(SummerLeagueDeskPlayerGrade).where(
                SummerLeagueDeskPlayerGrade.player_id == subject.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.competition_id == subject_comp.id,  # type: ignore[arg-type]
                SummerLeagueDeskPlayerGrade.baseline_version == version,  # type: ignore[arg-type]
            )
        )
    ).scalars().all()
    assert len(rows) == 1


async def test_grade_player_event_raises_for_unknown_competition(
    db_session: AsyncSession,
) -> None:
    player = await _seed_player(db_session, name="NoComp", draft_round=1, draft_pick=1)
    assert player.id is not None
    with pytest.raises(ValueError, match="summer_league_competitions"):
        await grade_player_event(
            db_session, player_id=player.id, competition_id=999999, baseline_version="v1"
        )


async def test_grade_player_event_raises_for_unknown_player(
    db_session: AsyncSession,
) -> None:
    comp = await _seed_competition(db_session, year=2024)
    assert comp.id is not None
    with pytest.raises(ValueError, match="players_master"):
        await grade_player_event(
            db_session, player_id=999999, competition_id=comp.id, baseline_version="v1"
        )


async def test_grade_player_event_raises_when_player_has_no_data_that_year(
    db_session: AsyncSession,
) -> None:
    comp = await _seed_competition(db_session, year=2024)
    player = await _seed_player(db_session, name="NoData", draft_round=1, draft_pick=1)
    assert comp.id is not None
    assert player.id is not None
    with pytest.raises(ValueError, match="No Summer League game data"):
        await grade_player_event(
            db_session, player_id=player.id, competition_id=comp.id, baseline_version="v1"
        )


async def test_grade_player_event_raises_when_no_active_baseline_for_cohort(
    db_session: AsyncSession,
) -> None:
    comp = await _seed_competition(db_session, year=2024)
    player = await _seed_player(db_session, name="NoBaseline", draft_round=1, draft_pick=1)
    await _seed_season(
        db_session, competition=comp, player=player, year=2024, gmsc=10.0, minutes=50.0, gp=3
    )
    assert comp.id is not None
    assert player.id is not None
    with pytest.raises(ValueError, match="No active T1 baseline"):
        await grade_player_event(
            db_session,
            player_id=player.id,
            competition_id=comp.id,
            baseline_version="nonexistent-version",
        )
