"""Integration tests for the Summer League Desk cohort-baseline builder (#502).

Seeds `SummerLeagueCompetition` + `SummerLeaguePlayerSeason` history plus draft
slots on `PlayerMaster`, runs `build_baselines`, and asserts the persisted T1
distributions and version-flip behavior end to end.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import SummerLeagueCompetition
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrain,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services.summer_league.cohort_baselines import build_baselines

pytestmark = pytest.mark.asyncio


async def _seed_competition(
    db: AsyncSession, *, year: int, venue_slug: str, league_id: str
) -> SummerLeagueCompetition:
    comp = SummerLeagueCompetition(
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
    competition: SummerLeagueCompetition,
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
        gp=gp,
        minutes=minutes,
        gmsc=gmsc,
    )
    db.add(season)
    await db.flush()
    return season


async def _active_rows(
    db: AsyncSession, version: str
) -> dict[str, SummerLeagueCohortBaseline]:
    stmt = select(SummerLeagueCohortBaseline).where(
        SummerLeagueCohortBaseline.baseline_version == version  # type: ignore[arg-type]
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {r.cohort_key: r for r in rows}


async def test_build_baselines_lottery_window_and_stats(
    db_session: AsyncSession,
) -> None:
    """Pick #1's ±3 window (clamped to 1-14) is its own slot:1-4 cohort.

    Each lottery pick centers its own window (pick 2's is 1-5, not 1-4), so
    the cohort a subject is ranked against is built from OTHER players who
    were also picked #1 across the history window -- not from picks 1-4
    collectively.
    """
    comp = await _seed_competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="13"
    )

    # Four different #1 picks (distinct player identities) all land in the
    # same slot:1-4 cohort.
    scores = [10.0, 20.0, 30.0, 40.0]
    for i, gmsc in enumerate(scores):
        player = await _seed_player(
            db_session, name=f"Pick1_{i}", draft_round=1, draft_pick=1
        )
        await _seed_season(
            db_session,
            competition=comp,
            player=player,
            year=2024,
            gmsc=gmsc,
            minutes=100.0,
            gp=5,
        )

    version = await build_baselines(
        db_session, season_range="2024-2024", min_minutes=40.0
    )
    await db_session.flush()

    rows = await _active_rows(db_session, version)
    slot_1_4 = rows["slot:1-4"]
    assert slot_1_4.cohort_kind == SummerLeagueDeskCohortKind.SLOT_WINDOW
    assert slot_1_4.grain == SummerLeagueDeskGrain.EVENT
    assert slot_1_4.slot_low == 1
    assert slot_1_4.slot_high == 4
    assert slot_1_4.n_members == 4
    assert slot_1_4.mean_value == 25.0
    assert slot_1_4.median_value == 25.0
    assert slot_1_4.breakpoints["50"] == 25.0
    assert slot_1_4.is_active is True


async def test_build_baselines_round_bucket_and_undrafted_and_debut(
    db_session: AsyncSession,
) -> None:
    """R1-late, R2, undrafted cohorts populate correctly, plus a debut-grain row."""
    comp = await _seed_competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="13"
    )

    r1_late = await _seed_player(
        db_session, name="R1Late", draft_round=1, draft_pick=20
    )
    await _seed_season(
        db_session,
        competition=comp,
        player=r1_late,
        year=2024,
        gmsc=15.0,
        minutes=100.0,
        gp=5,
    )

    # R2's within-round pick is 1-30 (overall picks 31-60) -- NOT pick<=14.
    r2 = await _seed_player(db_session, name="R2", draft_round=2, draft_pick=5)
    await _seed_season(
        db_session,
        competition=comp,
        player=r2,
        year=2024,
        gmsc=5.0,
        minutes=100.0,
        gp=5,
    )

    undrafted = await _seed_player(
        db_session, name="Undrafted", draft_round=None, draft_pick=None
    )
    await _seed_season(
        db_session,
        competition=comp,
        player=undrafted,
        year=2024,
        gmsc=8.0,
        minutes=100.0,
        gp=5,
    )

    version = await build_baselines(
        db_session, season_range="2024-2024", min_minutes=40.0
    )
    await db_session.flush()
    rows = await _active_rows(db_session, version)

    r1_late_row = rows["round:1_late"]
    assert r1_late_row.cohort_kind == SummerLeagueDeskCohortKind.ROUND_BUCKET
    assert (r1_late_row.slot_low, r1_late_row.slot_high) == (15, 30)
    assert r1_late_row.n_members == 1
    assert r1_late_row.mean_value == 15.0

    r2_row = rows["round:2"]
    assert r2_row.cohort_kind == SummerLeagueDeskCohortKind.ROUND_BUCKET
    # Round 2's within-round 1-30 window == overall picks 31-60.
    assert (r2_row.slot_low, r2_row.slot_high) == (31, 60)
    assert r2_row.mean_value == 5.0

    undrafted_row = rows["status:undrafted"]
    assert undrafted_row.cohort_kind == SummerLeagueDeskCohortKind.STATUS
    assert undrafted_row.slot_low is None
    assert undrafted_row.slot_high is None
    assert undrafted_row.mean_value == 8.0

    # Every player's only year is also their debut year.
    debut_row = rows["debut:1_late"]
    assert debut_row.cohort_kind == SummerLeagueDeskCohortKind.DEBUT
    assert debut_row.grain == SummerLeagueDeskGrain.DEBUT
    assert debut_row.n_members == 1
    assert debut_row.mean_value == 15.0


async def test_build_baselines_debut_grain_uses_earliest_year_only(
    db_session: AsyncSession,
) -> None:
    """A sophomore's return year feeds the event cohort but not the debut cohort."""
    comp_2023 = await _seed_competition(
        db_session, year=2023, venue_slug="las_vegas", league_id="13"
    )
    comp_2024 = await _seed_competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="13"
    )

    player = await _seed_player(
        db_session, name="Sophomore", draft_round=1, draft_pick=1
    )
    await _seed_season(
        db_session,
        competition=comp_2023,
        player=player,
        year=2023,
        gmsc=10.0,
        minutes=100.0,
        gp=5,
    )
    await _seed_season(
        db_session,
        competition=comp_2024,
        player=player,
        year=2024,
        gmsc=30.0,
        minutes=100.0,
        gp=5,
    )

    version = await build_baselines(
        db_session, season_range="2023-2024", min_minutes=40.0
    )
    await db_session.flush()
    rows = await _active_rows(db_session, version)

    # Event grain: both years count (2 events).
    event_row = rows["slot:1-4"]
    assert event_row.n_members == 2
    assert event_row.mean_value == 20.0  # (10 + 30) / 2

    # Debut grain: only the earliest year (2023, GmSc 10.0) counts.
    debut_row = rows["debut:1-4"]
    assert debut_row.n_members == 1
    assert debut_row.mean_value == 10.0


async def test_build_baselines_min_minutes_gate_excludes_thin_samples(
    db_session: AsyncSession,
) -> None:
    """A player-year under the minutes floor never enters the distribution."""
    comp = await _seed_competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="13"
    )
    thin = await _seed_player(db_session, name="Thin", draft_round=1, draft_pick=1)
    await _seed_season(
        db_session,
        competition=comp,
        player=thin,
        year=2024,
        gmsc=99.0,
        minutes=5.0,
        gp=1,
    )
    qualified = await _seed_player(
        db_session, name="Qualified", draft_round=1, draft_pick=2
    )
    await _seed_season(
        db_session,
        competition=comp,
        player=qualified,
        year=2024,
        gmsc=11.0,
        minutes=50.0,
        gp=5,
    )

    version = await build_baselines(
        db_session, season_range="2024-2024", min_minutes=40.0
    )
    await db_session.flush()
    rows = await _active_rows(db_session, version)

    # The thin sample (pick 1, 5 minutes) never enters ANY cohort -- the gate
    # drops it before a cohort_key is even assigned. Only pick 2's own
    # slot:1-5 window shows up, with just the qualified member.
    assert rows["slot:1-5"].n_members == 1
    assert rows["slot:1-5"].mean_value == 11.0


async def test_build_baselines_no_qualifying_events_raises(
    db_session: AsyncSession,
) -> None:
    """An empty history refuses to write an empty baseline_version."""
    with pytest.raises(ValueError):
        await build_baselines(db_session, season_range="1999-1999", min_minutes=40.0)


async def test_build_baselines_idempotent_rerun_flips_active_without_corrupting(
    db_session: AsyncSession,
) -> None:
    """Re-running writes a new version and deactivates the old one intact."""
    comp = await _seed_competition(
        db_session, year=2024, venue_slug="las_vegas", league_id="13"
    )
    player = await _seed_player(db_session, name="Solo", draft_round=1, draft_pick=1)
    await _seed_season(
        db_session,
        competition=comp,
        player=player,
        year=2024,
        gmsc=12.0,
        minutes=100.0,
        gp=5,
    )

    version_1 = await build_baselines(
        db_session, season_range="2024-2024", min_minutes=40.0
    )
    await db_session.flush()
    v1_rows = await _active_rows(db_session, version_1)
    assert all(r.is_active for r in v1_rows.values())
    v1_slot_row_id = v1_rows["slot:1-4"].id
    v1_mean = v1_rows["slot:1-4"].mean_value

    version_2 = await build_baselines(
        db_session, season_range="2024-2024", min_minutes=40.0
    )
    await db_session.flush()
    assert version_2 != version_1

    v2_rows = await _active_rows(db_session, version_2)
    assert all(r.is_active for r in v2_rows.values())

    # The old version's rows still exist, unmutated in content, but inactive.
    stmt = select(SummerLeagueCohortBaseline).where(
        SummerLeagueCohortBaseline.id == v1_slot_row_id  # type: ignore[arg-type]
    )
    old_row = (await db_session.execute(stmt)).scalar_one()
    assert old_row.is_active is False
    assert old_row.baseline_version == version_1
    assert old_row.mean_value == v1_mean
