"""Integration tests for the Summer League Desk readiness checker (#536).

`scripts/check_sl_desk_readiness.py` gates enabling the Desk cron in two
places: ``preflight`` (before the cron machine exists/is enabled) and
``post-tick`` (right after a deliberate one-time manual tick). These tests
prove the exact behavioral contract the launch-readiness ticket -- and its
follow-up hardening fixes -- call for:

* A fully-seeded valid state exits 0 (``report.ok``) in both modes without
  writing anything -- no row is ever added/updated by the checker itself.
* Each missing prerequisite category ("registration", "baselines",
  "freshness", "render_snapshots") fails on its own, with a distinct
  message identifying exactly what's missing, while every other category
  in that same seeded state still passes.
* Freshness is asymmetric by mode: a present-but-stale ``event_desk_state``
  row is only informational (``skip``) in ``preflight`` -- never a hard
  failure, since that would deadlock cron enablement on the very state the
  cron would refresh -- but is a hard ``fail`` in ``post-tick``.
* Render snapshots are asymmetric too: ``preflight`` stays "when present"
  (zero snapshots is fine; a stale ``schema_version`` on rows that exist is
  not). ``post-tick`` requires the COMPLETE materialized variant matrix
  (``DESK_RENDER_DAILY_STATES`` x ``TRACKER_COHORTS`` x
  ``TRACKER_STAT_VIEWS``, #551) at the current ``schema_version`` -- zero
  snapshots, a partial matrix, or a stale ``schema_version`` all fail.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import (
    Event,
    EventCalendarSource,
    EventDailyState,
    EventDeskState,
    EventLifecyclePhase,
    EventType,
)
from app.schemas.event_desk_render_snapshot import EventDeskRenderSnapshot
from app.schemas.summer_league import SummerLeagueCompetition
from app.schemas.summer_league_desk import (
    SummerLeagueCohortBaseline,
    SummerLeagueDeskCohortKind,
    SummerLeagueDeskGrain,
)
from app.services.event_desk.render_snapshots import CURRENT_SCHEMA_VERSION
from app.services.summer_league.scoreboard_ingest import EVENT_KEY_SUMMER_LEAGUE
from scripts.check_sl_desk_readiness import (
    EXPECTED_RENDER_VARIANTS,
    REQUIRED_BASELINE_GRAINS,
    ReadinessStatus,
    build_readiness_report,
)

pytestmark = pytest.mark.asyncio

_NOW = datetime(
    2026, 7, 10, 20, 0
)  # 4:00pm ET (EDT, UTC-4) -- mirrors test_sl_desk_tick.py


async def _seed_competition(db: AsyncSession, *, year: int) -> SummerLeagueCompetition:
    comp = SummerLeagueCompetition(
        year=year,
        league_id="15",
        venue_slug="las_vegas",
        display_name=f"{year} Las Vegas",
        starts_on=date(year, 7, 1),
        ends_on=date(year, 7, 20),
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def _seed_baseline(
    db: AsyncSession, *, grain: SummerLeagueDeskGrain, baseline_version: str = "v1"
) -> None:
    db.add(
        SummerLeagueCohortBaseline(
            baseline_version=baseline_version,
            is_active=True,
            cohort_key=f"{grain.value}:slot:1-4",
            cohort_kind=SummerLeagueDeskCohortKind.SLOT_WINDOW,
            metric="gmsc",
            grain=grain,
            venue_scope="all",
            season_range="2017-2025",
            min_minutes=40.0,
            n_members=20,
            breakpoints={"0": 10.0, "50": 50.0, "100": 90.0},
            mean_value=50.0,
            median_value=50.0,
        )
    )
    await db.flush()


async def _seed_all_required_baselines(db: AsyncSession) -> None:
    for grain in REQUIRED_BASELINE_GRAINS:
        await _seed_baseline(db, grain=grain)


async def _seed_event(
    db: AsyncSession,
    *,
    competition: SummerLeagueCompetition,
    empty_calendar_ref: bool = False,
) -> Event:
    assert competition.id is not None
    event = Event(
        key=EVENT_KEY_SUMMER_LEAGUE,
        name="Summer League",
        event_type=EventType.PRO_SUMMER,
        calendar_source=EventCalendarSource.SCHEDULE,
        calendar_ref={}
        if empty_calendar_ref
        else {"competition_ids": [competition.id]},
        window_priors={},
        cohort_basis="slot_window",
        priority=100,
        is_active=True,
    )
    db.add(event)
    await db.flush()
    assert event.id is not None
    return event


async def _seed_state(
    db: AsyncSession, *, event: Event, freshness_tick_at: datetime
) -> EventDeskState:
    assert event.id is not None
    state = EventDeskState(
        event_id=event.id,
        as_of=freshness_tick_at,
        lifecycle_phase=EventLifecyclePhase.ACTIVE,
        daily_state=None,
        is_home_owner=True,
        freshness_tick_at=freshness_tick_at,
        next_tick_eta=freshness_tick_at + timedelta(hours=1),
    )
    db.add(state)
    await db.flush()
    return state


async def _seed_render_snapshot(
    db: AsyncSession, *, event: Event, schema_version: int
) -> EventDeskRenderSnapshot:
    """Seed a single render snapshot row (used for preflight "when present" cases)."""
    assert event.id is not None
    snapshot = EventDeskRenderSnapshot(
        event_id=event.id,
        daily_state=EventDailyState.LIVE,
        tracker_cohort="all",
        tracker_stat_view="per_game",
        schema_version=schema_version,
        payload_json=None,
        view_context_json={},
    )
    db.add(snapshot)
    await db.flush()
    return snapshot


async def _seed_full_render_snapshot_matrix(
    db: AsyncSession,
    *,
    event: Event,
    schema_version: int = CURRENT_SCHEMA_VERSION,
    skip_variants: frozenset[tuple[EventDailyState, str, str]] = frozenset(),
) -> None:
    """Seed the COMPLETE post-#551 render snapshot matrix (`EXPECTED_RENDER_VARIANTS`).

    ``skip_variants`` lets a caller seed a deliberately partial matrix (to test the
    post-tick "missing variants" failure) by omitting specific `(daily_state,
    tracker_cohort, tracker_stat_view)` combinations.
    """
    assert event.id is not None
    for daily_state, cohort, stat_view in EXPECTED_RENDER_VARIANTS:
        if (daily_state, cohort, stat_view) in skip_variants:
            continue
        db.add(
            EventDeskRenderSnapshot(
                event_id=event.id,
                daily_state=daily_state,
                tracker_cohort=cohort,
                tracker_stat_view=stat_view,
                schema_version=schema_version,
                payload_json=None,
                view_context_json={},
            )
        )
    await db.flush()


def _result_for(report, category: str):
    """Return the single :class:`CheckResult` for ``category`` (fails the test if absent)."""
    matches = [r for r in report.results if r.category == category]
    assert len(matches) == 1, f"expected exactly one '{category}' result, got {matches}"
    return matches[0]


async def _assert_no_writes(db: AsyncSession, *, before: dict[str, int]) -> None:
    """Assert row counts for every readiness-relevant table are unchanged."""
    after = {
        "events": len((await db.execute(select(Event))).scalars().all()),
        "event_desk_state": len(
            (await db.execute(select(EventDeskState))).scalars().all()
        ),
        "render_snapshots": len(
            (await db.execute(select(EventDeskRenderSnapshot))).scalars().all()
        ),
        "competitions": len(
            (await db.execute(select(SummerLeagueCompetition))).scalars().all()
        ),
        "baselines": len(
            (await db.execute(select(SummerLeagueCohortBaseline))).scalars().all()
        ),
    }
    assert after == before


async def _table_counts(db: AsyncSession) -> dict[str, int]:
    return {
        "events": len((await db.execute(select(Event))).scalars().all()),
        "event_desk_state": len(
            (await db.execute(select(EventDeskState))).scalars().all()
        ),
        "render_snapshots": len(
            (await db.execute(select(EventDeskRenderSnapshot))).scalars().all()
        ),
        "competitions": len(
            (await db.execute(select(SummerLeagueCompetition))).scalars().all()
        ),
        "baselines": len(
            (await db.execute(select(SummerLeagueCohortBaseline))).scalars().all()
        ),
    }


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------


async def test_preflight_valid_state_exits_zero_no_writes(
    db_session: AsyncSession,
) -> None:
    """Competitions + all 3 baseline grains seeded -> preflight passes with no writes."""
    competition = await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    await db_session.commit()

    before = await _table_counts(db_session)
    report = await build_readiness_report(db_session, mode="preflight", now=_NOW)
    await db_session.commit()

    assert report.ok is True
    assert _result_for(report, "registration").status == ReadinessStatus.PASS
    assert _result_for(report, "baselines").status == ReadinessStatus.PASS
    # No tick has run yet: freshness/render_snapshots are informational skips,
    # not failures, and must not be required for preflight to pass.
    assert _result_for(report, "freshness").status == ReadinessStatus.SKIP
    assert _result_for(report, "render_snapshots").status == ReadinessStatus.SKIP

    await _assert_no_writes(db_session, before=before)
    assert competition.id is not None  # sanity: fixture actually persisted


async def test_preflight_missing_competitions_exits_one_with_distinct_message(
    db_session: AsyncSession,
) -> None:
    """No competitions registered for the current year -> distinct registration failure."""
    await _seed_all_required_baselines(db_session)
    await db_session.commit()

    report = await build_readiness_report(db_session, mode="preflight", now=_NOW)

    assert report.ok is False
    registration = _result_for(report, "registration")
    assert registration.status == ReadinessStatus.FAIL
    assert "No Summer League competitions configured" in registration.message
    assert str(_NOW.year) in registration.message
    # The other, independently-seeded category still passes.
    assert _result_for(report, "baselines").status == ReadinessStatus.PASS


async def test_preflight_missing_baseline_grain_exits_one_with_distinct_message(
    db_session: AsyncSession,
) -> None:
    """Job A ran but only produced event+debut grains (game grain, #525, missing)."""
    await _seed_competition(db_session, year=_NOW.year)
    await _seed_baseline(db_session, grain=SummerLeagueDeskGrain.EVENT)
    await _seed_baseline(db_session, grain=SummerLeagueDeskGrain.DEBUT)
    await db_session.commit()

    report = await build_readiness_report(db_session, mode="preflight", now=_NOW)

    assert report.ok is False
    baselines = _result_for(report, "baselines")
    assert baselines.status == ReadinessStatus.FAIL
    assert "game" in baselines.message
    assert "build_sl_cohort_baselines.py" in baselines.message
    # event/debut grains being present doesn't leak into a false pass elsewhere.
    assert _result_for(report, "registration").status == ReadinessStatus.PASS


# --------------------------------------------------------------------------
# post-tick
# --------------------------------------------------------------------------


async def test_post_tick_valid_state_exits_zero_no_writes(
    db_session: AsyncSession,
) -> None:
    """A fully-ticked, fresh, correctly-versioned state passes post-tick with no writes."""
    competition = await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    event = await _seed_event(db_session, competition=competition)
    await _seed_state(db_session, event=event, freshness_tick_at=_NOW)
    await _seed_full_render_snapshot_matrix(db_session, event=event)
    await db_session.commit()

    before = await _table_counts(db_session)
    report = await build_readiness_report(db_session, mode="post-tick", now=_NOW)
    await db_session.commit()

    assert report.ok is True
    assert _result_for(report, "registration").status == ReadinessStatus.PASS
    assert _result_for(report, "baselines").status == ReadinessStatus.PASS
    assert _result_for(report, "freshness").status == ReadinessStatus.PASS
    assert _result_for(report, "render_snapshots").status == ReadinessStatus.PASS

    await _assert_no_writes(db_session, before=before)


async def test_post_tick_missing_registration_exits_one_with_distinct_message(
    db_session: AsyncSession,
) -> None:
    """Competitions/baselines ready but the tick never synced an 'events' row."""
    await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    await db_session.commit()

    report = await build_readiness_report(db_session, mode="post-tick", now=_NOW)

    assert report.ok is False
    registration = _result_for(report, "registration")
    assert registration.status == ReadinessStatus.FAIL
    assert "events" in registration.message
    assert "Job B" in registration.message
    assert _result_for(report, "baselines").status == ReadinessStatus.PASS


async def test_post_tick_empty_calendar_ref_exits_one_with_distinct_message(
    db_session: AsyncSession,
) -> None:
    """An 'events' row exists but its calendar_ref never got populated with competitions."""
    competition = await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    await _seed_event(db_session, competition=competition, empty_calendar_ref=True)
    await db_session.commit()

    report = await build_readiness_report(db_session, mode="post-tick", now=_NOW)

    assert report.ok is False
    registration = _result_for(report, "registration")
    assert registration.status == ReadinessStatus.FAIL
    assert "calendar_ref" in registration.message


async def test_post_tick_stale_freshness_exits_one_with_distinct_message(
    db_session: AsyncSession,
) -> None:
    """event_desk_state exists but its freshness stamp is older than the staleness threshold."""
    competition = await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    event = await _seed_event(db_session, competition=competition)
    stale_tick = _NOW - timedelta(hours=5)
    await _seed_state(db_session, event=event, freshness_tick_at=stale_tick)
    await db_session.commit()

    report = await build_readiness_report(
        db_session, mode="post-tick", now=_NOW, staleness_hours=2.0
    )

    assert report.ok is False
    freshness = _result_for(report, "freshness")
    assert freshness.status == ReadinessStatus.FAIL
    assert "stale" in freshness.message
    # Registration/baselines are unaffected by the stale freshness stamp.
    assert _result_for(report, "registration").status == ReadinessStatus.PASS
    assert _result_for(report, "baselines").status == ReadinessStatus.PASS


async def test_post_tick_missing_state_exits_one_with_distinct_message(
    db_session: AsyncSession,
) -> None:
    """An 'events' row synced but Job B's step 6 (event_desk_state upsert) never ran."""
    competition = await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    await _seed_event(db_session, competition=competition)
    await db_session.commit()

    report = await build_readiness_report(db_session, mode="post-tick", now=_NOW)

    assert report.ok is False
    freshness = _result_for(report, "freshness")
    assert freshness.status == ReadinessStatus.FAIL
    assert "event_desk_state" in freshness.message


async def test_post_tick_stale_render_snapshot_schema_exits_one_with_distinct_message(
    db_session: AsyncSession,
) -> None:
    """The full matrix is materialized, but one row carries an old schema_version."""
    competition = await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    event = await _seed_event(db_session, competition=competition)
    await _seed_state(db_session, event=event, freshness_tick_at=_NOW)
    await _seed_full_render_snapshot_matrix(db_session, event=event)
    # Overwrite one row with a stale schema_version -- everything else in the
    # matrix stays complete and current.
    assert event.id is not None
    stale_variant = next(iter(EXPECTED_RENDER_VARIANTS))
    daily_state, cohort, stat_view = stale_variant
    stmt = select(EventDeskRenderSnapshot).where(
        EventDeskRenderSnapshot.event_id == event.id,
        EventDeskRenderSnapshot.daily_state == daily_state,
        EventDeskRenderSnapshot.tracker_cohort == cohort,
        EventDeskRenderSnapshot.tracker_stat_view == stat_view,
    )
    row = (await db_session.execute(stmt)).scalar_one()
    row.schema_version = CURRENT_SCHEMA_VERSION + 999
    db_session.add(row)
    await db_session.commit()

    report = await build_readiness_report(db_session, mode="post-tick", now=_NOW)

    assert report.ok is False
    snapshots = _result_for(report, "render_snapshots")
    assert snapshots.status == ReadinessStatus.FAIL
    assert "schema_version" in snapshots.message
    # The matrix is complete (no missing variants) -- only staleness is wrong.
    assert "missing" not in snapshots.message
    # Everything else about this state is otherwise healthy.
    assert _result_for(report, "registration").status == ReadinessStatus.PASS
    assert _result_for(report, "baselines").status == ReadinessStatus.PASS
    assert _result_for(report, "freshness").status == ReadinessStatus.PASS


async def test_post_tick_no_render_snapshots_exits_one_with_distinct_message(
    db_session: AsyncSession,
) -> None:
    """#551 made full matrix materialization mandatory -- zero snapshots is now a failure."""
    competition = await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    event = await _seed_event(db_session, competition=competition)
    await _seed_state(db_session, event=event, freshness_tick_at=_NOW)
    await db_session.commit()

    report = await build_readiness_report(db_session, mode="post-tick", now=_NOW)

    assert report.ok is False
    snapshots = _result_for(report, "render_snapshots")
    assert snapshots.status == ReadinessStatus.FAIL
    assert "0 render snapshots" in snapshots.message
    # Everything else about this state is otherwise healthy.
    assert _result_for(report, "registration").status == ReadinessStatus.PASS
    assert _result_for(report, "baselines").status == ReadinessStatus.PASS
    assert _result_for(report, "freshness").status == ReadinessStatus.PASS


async def test_post_tick_partial_render_snapshot_matrix_names_missing_variants(
    db_session: AsyncSession,
) -> None:
    """A partial matrix (some variants never materialized) fails, naming what's missing."""
    competition = await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    event = await _seed_event(db_session, competition=competition)
    await _seed_state(db_session, event=event, freshness_tick_at=_NOW)
    missing_variant = next(iter(EXPECTED_RENDER_VARIANTS))
    await _seed_full_render_snapshot_matrix(
        db_session, event=event, skip_variants=frozenset({missing_variant})
    )
    await db_session.commit()

    report = await build_readiness_report(db_session, mode="post-tick", now=_NOW)

    assert report.ok is False
    snapshots = _result_for(report, "render_snapshots")
    assert snapshots.status == ReadinessStatus.FAIL
    assert "missing 1/" in snapshots.message
    daily_state, cohort, stat_view = missing_variant
    assert f"({daily_state.value}, {cohort}, {stat_view})" in snapshots.message
    # Everything else about this state is otherwise healthy.
    assert _result_for(report, "registration").status == ReadinessStatus.PASS
    assert _result_for(report, "baselines").status == ReadinessStatus.PASS
    assert _result_for(report, "freshness").status == ReadinessStatus.PASS


async def test_post_tick_full_render_snapshot_matrix_passes(
    db_session: AsyncSession,
) -> None:
    """The COMPLETE variant matrix at the current schema_version -- render_snapshots passes."""
    competition = await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    event = await _seed_event(db_session, competition=competition)
    await _seed_state(db_session, event=event, freshness_tick_at=_NOW)
    await _seed_full_render_snapshot_matrix(db_session, event=event)
    await db_session.commit()

    report = await build_readiness_report(db_session, mode="post-tick", now=_NOW)

    assert report.ok is True
    snapshots = _result_for(report, "render_snapshots")
    assert snapshots.status == ReadinessStatus.PASS
    assert str(len(EXPECTED_RENDER_VARIANTS)) in snapshots.message


async def test_preflight_render_snapshots_zero_is_skip(
    db_session: AsyncSession,
) -> None:
    """Preflight keeps the lenient "when present" behavior: zero snapshots -> skip."""
    competition = await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    event = await _seed_event(db_session, competition=competition)
    await db_session.commit()
    assert event.id is not None

    report = await build_readiness_report(db_session, mode="preflight", now=_NOW)

    assert report.ok is True
    assert _result_for(report, "render_snapshots").status == ReadinessStatus.SKIP


async def test_preflight_render_snapshot_present_and_current_passes(
    db_session: AsyncSession,
) -> None:
    """Preflight's "when present" leniency doesn't require the full post-tick matrix."""
    competition = await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    event = await _seed_event(db_session, competition=competition)
    await _seed_render_snapshot(
        db_session, event=event, schema_version=CURRENT_SCHEMA_VERSION
    )
    await db_session.commit()

    report = await build_readiness_report(db_session, mode="preflight", now=_NOW)

    assert report.ok is True
    assert _result_for(report, "render_snapshots").status == ReadinessStatus.PASS


async def test_preflight_render_snapshot_stale_schema_fails(
    db_session: AsyncSession,
) -> None:
    """Preflight still fails a stale schema_version on a snapshot that DOES exist."""
    competition = await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    event = await _seed_event(db_session, competition=competition)
    await _seed_render_snapshot(
        db_session, event=event, schema_version=CURRENT_SCHEMA_VERSION + 999
    )
    await db_session.commit()

    report = await build_readiness_report(db_session, mode="preflight", now=_NOW)

    assert report.ok is False
    snapshots = _result_for(report, "render_snapshots")
    assert snapshots.status == ReadinessStatus.FAIL
    assert "schema_version" in snapshots.message


async def test_preflight_stale_freshness_is_skip_not_fail(
    db_session: AsyncSession,
) -> None:
    """A present-but-stale event_desk_state row must not deadlock preflight.

    The deploy workflow gates cron *enablement* on preflight passing; if a stale
    prior state (e.g. left over from a redeploy/recovery, or a cron that was down)
    hard-failed preflight, it would block creating the very cron that would refresh
    it. Staleness is informational (skip) pre-tick -- only post-tick fails on it.
    """
    competition = await _seed_competition(db_session, year=_NOW.year)
    await _seed_all_required_baselines(db_session)
    event = await _seed_event(db_session, competition=competition)
    stale_tick = _NOW - timedelta(hours=5)
    await _seed_state(db_session, event=event, freshness_tick_at=stale_tick)
    await db_session.commit()

    before = await _table_counts(db_session)
    report = await build_readiness_report(
        db_session, mode="preflight", now=_NOW, staleness_hours=2.0
    )
    await db_session.commit()

    assert report.ok is True
    freshness = _result_for(report, "freshness")
    assert freshness.status == ReadinessStatus.SKIP
    assert "stale" in freshness.message
    # Registration/baselines are unaffected by the stale freshness stamp.
    assert _result_for(report, "registration").status == ReadinessStatus.PASS
    assert _result_for(report, "baselines").status == ReadinessStatus.PASS

    await _assert_no_writes(db_session, before=before)
