"""Acceptance tests for the Desk latency-class partition (#699).

These are the tests that make #699 shippable off-season. The change's whole
value is reliability *under live-game load*, and Summer League 2026 ended
2026-07-19 -- so rather than merge unverified code that first runs during the
event it exists to protect, the partition is proven here against a replay of a
real recorded event window (``tests/integration/_desk_replay.py``).

The property under test, from
`docs/plans/summer-league-desk-simplification-spec.md` §2:

    The fast path never waits on the slow path. It writes a narrow,
    well-scoped set of canonical rows and takes no global lock.

and the acceptance signal:

    The percentage of scheduled ticks that complete with advanced source
    data, measured **specifically within live-game windows** -- not averaged
    across the day.

The suite is deliberately built as a **matched pair**, because a test showing
the new code works proves much less than one that also shows the old code
fails the same check:

* :func:`test_fast_class_lands_every_live_window_tick_while_backbone_holds_lock`
  -- the fast class replays a real live window at 100% while a stand-in
  backbone holds the shared writer lock throughout.
* :func:`test_composite_tick_is_starved_by_the_same_contention`
  -- the pre-#699 composite, run against the *same* held lock, times out and
  skips its interval. This is the failure being fixed, reproduced on demand.

Without the second test the first is unfalsifiable: a fast class that happened
never to contend would pass it just as happily.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.event_desk import EventDailyState, EventDeskState
from app.schemas.summer_league import SummerLeagueGame, SummerLeagueGameStatus
from app.schemas.summer_league_pipeline import SummerLeaguePipelineJob
from app.services.summer_league.desk_tick.backbone import run_backbone_tick
from app.services.summer_league.desk_tick.composite import run_desk_tick
from app.services.summer_league.desk_tick.fast import run_fast_tick
from app.services.summer_league.desk_tick.projection import run_projection_tick
from app.services.summer_league.desk_tick.shared import (
    NO_WRITER_LOCK,
    DeskLatencyClass,
    TickContext,
    WriterLockPolicy,
)
from app.services.summer_league.nba_stats_client import NBAStatsClient
from app.services.summer_league.pipeline_telemetry import PipelineTelemetry
from app.services.summer_league.write_lock import SummerLeagueWriterLockTimeout
from tests.integration._desk_replay import (
    CAPTURE_REFERENCE_NOW,
    ReplaySession,
    backbone_holding_writer_lock,
    derived_in_progress_frame,
    real_live_window_frames,
    replay_frames,
)
from tests.integration.test_sl_desk_tick import (
    _seed_baseline,
    _seed_competition,
    _seed_game,
    _seed_team,
)

pytestmark = pytest.mark.asyncio

# The fast class's latency budget is "seconds" (spec §2). Ten seconds per
# frame is a deliberately loose ceiling for a shared CI box -- it is not a
# performance target, it is a tripwire for the frame having *queued* on
# something, which is the failure mode this ticket removes. The real signal is
# that it lands at all while the lock is held.
FAST_FRAME_BUDGET_SECONDS = 10.0

# How long the stand-in backbone holds the writer lock. Production's was ~88
# minutes; this only has to outlast the whole replay for the contention to be
# genuine.
LOCK_HOLD_SECONDS = 20.0


async def _seed_live_window(db: AsyncSession) -> None:
    """Seed a 2026 competition anchored in the captured live window.

    The anchor game exists so ``resolve_daily_state`` resolves non-dormant on
    the first call, keeping these tests about contention rather than the
    separately-covered #527 bootstrap path.
    """
    competition = await _seed_competition(db, year=2026, league_id="15")
    home = await _seed_team(db, competition)
    away = await _seed_team(db, competition)
    await _seed_game(
        db,
        competition,
        home,
        away,
        game_date=date(2026, 7, 10),
        tip_datetime=datetime(2026, 7, 10, 18, 0),
        status=SummerLeagueGameStatus.FINAL,
    )
    await _seed_baseline(db, baseline_version="sl-desk-699-latency-classes")
    await db.commit()


async def _pin_schema(db: AsyncSession, test_schema: str) -> None:
    """Pin this session to a connection primed with ``test_schema``.

    Without this, the connection SQLAlchemy autobegins the first time the tick
    touches the session can be a brand-new, un-primed connection whose
    ``current_schema()`` isn't ``test_schema`` -- silently keying its
    advisory-lock attempts off the wrong schema, so the two sessions contend on
    unrelated locks and the test proves nothing.
    """
    await db.execute(text(f'SET search_path TO "{test_schema}"'))


@pytest.mark.committed_db
async def test_fast_class_lands_every_live_window_tick_while_backbone_holds_lock(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
    tmp_path: Path,
) -> None:
    """#699 acceptance: the fast class is not starved by the backbone.

    Replays six frames of a **real captured** 2026 live window through the
    fast class while a stand-in backbone holds the shared Summer League writer
    lock for the entire replay. Every frame must complete, and every frame
    must complete *with advanced source data* -- spec §2's success metric,
    reported for the live window specifically and never daily-averaged.

    All six frames are verbatim provider content (``derived=False``); the only
    thing that changes between them is the tick's clock, which is exactly how a
    real poller experiences an evening. The acceptance signal therefore rests
    on zero fabricated data.
    """
    await _seed_live_window(db_session)
    await _pin_schema(db_session, test_schema)

    session = ReplaySession()
    client = NBAStatsClient(session=session)
    frames = real_live_window_frames(count=6, step=timedelta(minutes=10))
    assert all(not frame.derived for frame in frames), (
        "the acceptance signal must not depend on fabricated provider content"
    )

    async def _run_frame(frame: object) -> object:
        result = await run_fast_tick(
            db_session,
            TickContext.for_run(
                now=frame.now,  # type: ignore[attr-defined]
                raw_root=tmp_path,
                client=client,
                lock=NO_WRITER_LOCK,
            ),
        )
        await db_session.commit()
        return result

    async with backbone_holding_writer_lock(
        session_factory, test_schema, hold_seconds=LOCK_HOLD_SECONDS
    ) as holder:
        outcome = await replay_frames(frames, session, run_frame=_run_frame)
        # The contention must have been real for the whole replay -- if the
        # holder had already exited, this test would pass vacuously.
        assert not holder.done(), (
            "the stand-in backbone released the writer lock before the replay "
            "finished; the replay proved nothing about contention"
        )

    assert outcome.completion_rate == 1.0, outcome.describe()
    assert outcome.advanced_rate == 1.0, outcome.describe()
    assert outcome.slowest_seconds < FAST_FRAME_BUDGET_SECONDS, outcome.describe()

    # Every frame issued a genuinely fresh round of provider calls rather than
    # replaying a cached response -- otherwise "six frames completed" could be
    # one real poll and five no-ops, and the completion rate would be theater.
    schedule_calls = [
        call for call in session.calls if call[0].endswith("scheduleleaguev2")
    ]
    assert len(schedule_calls) == len(frames), (
        f"expected one scoreboard fetch per frame, got {len(schedule_calls)}"
    )

    # The narrow canonical write set actually landed: the capture's real Final
    # games are persisted with their real scores.
    row = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522600001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert row.status == SummerLeagueGameStatus.FINAL
    assert (row.home_score, row.away_score) == (92, 105)


@pytest.mark.committed_db
async def test_composite_tick_is_starved_by_the_same_contention(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
    tmp_path: Path,
) -> None:
    """The control: pre-#699 behavior fails the check the fast class passes.

    Same seeded live window, same held writer lock -- but run through the
    composite orchestrator, which takes the lock across its whole run. It must
    time out and skip the interval, which from a visitor's side is
    indistinguishable from the Desk being broken.

    This is the production failure reproduced on demand. Its value is
    falsification: without it, the sibling test above could pass simply because
    nothing ever contended.
    """
    await _seed_live_window(db_session)
    await _pin_schema(db_session, test_schema)

    session = ReplaySession()
    session.use(real_live_window_frames(count=1)[0])
    client = NBAStatsClient(session=session)

    async with backbone_holding_writer_lock(
        session_factory, test_schema, hold_seconds=LOCK_HOLD_SECONDS
    ):
        with pytest.raises(SummerLeagueWriterLockTimeout):
            await run_desk_tick(
                db_session,
                now=CAPTURE_REFERENCE_NOW,
                raw_root=tmp_path,
                client=client,
                writer_lock_max_wait_seconds=0.5,
            )
    await db_session.rollback()


@pytest.mark.committed_db
async def test_the_lock_policy_is_what_saves_the_fast_class(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
    tmp_path: Path,
) -> None:
    """Mutation control: give the fast class a lock, and it starves too.

    The sibling acceptance test shows the fast class landing under contention;
    the composite control shows the old orchestrator failing. Neither on its
    own isolates *why* -- the composite also does more work, so its timeout
    could in principle be attributed to something else.

    This closes that gap by changing exactly one variable. Same class, same
    frames, same held lock, same everything -- except a lock-taking policy
    instead of :data:`NO_WRITER_LOCK`. It must now time out. That makes the
    lock policy, not incidental differences, the demonstrated cause of the
    fast class's reliability.
    """
    await _seed_live_window(db_session)
    await _pin_schema(db_session, test_schema)

    session = ReplaySession()
    session.use(real_live_window_frames(count=1)[0])
    client = NBAStatsClient(session=session)

    async with backbone_holding_writer_lock(
        session_factory, test_schema, hold_seconds=LOCK_HOLD_SECONDS
    ):
        with pytest.raises(SummerLeagueWriterLockTimeout):
            await run_fast_tick(
                db_session,
                TickContext.for_run(
                    now=CAPTURE_REFERENCE_NOW,
                    raw_root=tmp_path,
                    client=client,
                    lock=WriterLockPolicy(enabled=True, max_wait_seconds=0.5),
                ),
            )
    await db_session.rollback()


@pytest.mark.committed_db
async def test_fast_class_advances_scores_for_a_live_game_under_contention(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
    tmp_path: Path,
) -> None:
    """A game going live mid-replay still gets its score polled while the lock is held.

    Uses the harness's one **derived** frame (a real Scheduled game advanced to
    ``gameStatus == 2`` with partial scores) because no genuinely in-progress
    capture exists in this repo and this sandbox cannot fetch one. It is not
    the acceptance signal -- the sibling acceptance test uses only verbatim
    content -- it exists to show the specific user-visible thing the Desk
    promises during a live game: the number on the card moves.
    """
    await _seed_live_window(db_session)
    await _pin_schema(db_session, test_schema)

    session = ReplaySession()
    client = NBAStatsClient(session=session)

    # 1522600008 is Scheduled in the capture and inside the active window at
    # the reference instant (it is one of the six games #531's own tests
    # select).
    live_frame = derived_in_progress_frame(
        nba_stats_game_id="1522600008",
        now=CAPTURE_REFERENCE_NOW + timedelta(minutes=5),
        home_score=58,
        away_score=61,
    )
    assert live_frame.derived is True

    async with backbone_holding_writer_lock(
        session_factory, test_schema, hold_seconds=LOCK_HOLD_SECONDS
    ):
        session.use(live_frame)
        result = await run_fast_tick(
            db_session,
            TickContext.for_run(
                now=live_frame.now,
                raw_root=tmp_path,
                client=client,
                lock=NO_WRITER_LOCK,
            ),
        )
        await db_session.commit()

    assert result.dormant is False
    row = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522600008"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    assert row.status == SummerLeagueGameStatus.IN_PROGRESS
    assert (row.home_score, row.away_score) == (58, 61)


@pytest.mark.committed_db
async def test_projection_class_rebuilds_the_desk_while_backbone_holds_lock(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """The medium class needs no backbone lock either (spec §2).

    "Its only writes are to Desk projection tables it exclusively owns." So a
    backbone pass running long must not stop the hourly projection from
    landing -- the same starvation, one layer up.
    """
    await _seed_live_window(db_session)
    await _pin_schema(db_session, test_schema)

    async with backbone_holding_writer_lock(
        session_factory, test_schema, hold_seconds=LOCK_HOLD_SECONDS
    ) as holder:
        result = await run_projection_tick(
            db_session,
            TickContext.for_run(now=CAPTURE_REFERENCE_NOW, lock=NO_WRITER_LOCK),
        )
        await db_session.commit()
        assert not holder.done()

    assert result.dormant is False
    assert result.content_updated is True
    assert result.materialized_variant_count > 0
    assert result.daily_state in {
        EventDailyState.PREVIEW,
        EventDailyState.LIVE,
        EventDailyState.RECAP,
    }
    states = (await db_session.execute(select(EventDeskState))).scalars().all()
    assert states, "the projection class must have stamped event_desk_state"


@pytest.mark.committed_db
async def test_backbone_lock_timeout_leaves_fast_and_projection_unaffected(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
    tmp_path: Path,
) -> None:
    """#699 acceptance: classes fail independently.

    The backbone times out waiting for a held writer lock -- and the fast and
    projection classes, run immediately afterward against the *same* still-held
    lock, both land anyway. Before the partition these were one transaction, so
    the backbone's timeout took the entire user-visible Desk down with it.
    """
    await _seed_live_window(db_session)
    await _pin_schema(db_session, test_schema)

    session = ReplaySession()
    session.use(real_live_window_frames(count=1)[0])
    client = NBAStatsClient(session=session)

    async with backbone_holding_writer_lock(
        session_factory, test_schema, hold_seconds=LOCK_HOLD_SECONDS
    ):
        with pytest.raises(SummerLeagueWriterLockTimeout):
            await run_backbone_tick(
                db_session,
                TickContext.for_run(
                    now=CAPTURE_REFERENCE_NOW,
                    raw_root=tmp_path,
                    lock=WriterLockPolicy(enabled=True, max_wait_seconds=0.5),
                ),
            )
        await db_session.rollback()

        fast_result = await run_fast_tick(
            db_session,
            TickContext.for_run(
                now=CAPTURE_REFERENCE_NOW,
                raw_root=tmp_path,
                client=client,
                lock=NO_WRITER_LOCK,
            ),
        )
        await db_session.commit()

        projection_result = await run_projection_tick(
            db_session,
            TickContext.for_run(now=CAPTURE_REFERENCE_NOW, lock=NO_WRITER_LOCK),
        )
        await db_session.commit()

    assert fast_result.dormant is False
    assert fast_result.source_advanced is True
    assert projection_result.content_updated is True
    assert projection_result.materialized_variant_count > 0


@pytest.mark.committed_db
async def test_each_class_reports_under_its_own_telemetry_job(
    db_session: AsyncSession,
    test_schema: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#699 scope: "the tick was slow" must resolve to *which* class.

    Each class stamps its own ``job=`` label on every structured step record
    and owns its own ``summer_league_pipeline_states`` row, so a 40-minute
    backbone and a 3-second poll are never averaged into one number.
    """
    await _seed_live_window(db_session)
    await _pin_schema(db_session, test_schema)

    session = ReplaySession()
    session.use(real_live_window_frames(count=1)[0])
    client = NBAStatsClient(session=session)

    telemetry_logger = logging.getLogger("test_sl_desk_latency_classes")
    with caplog.at_level(logging.INFO, logger=telemetry_logger.name):
        await run_fast_tick(
            db_session,
            TickContext.for_run(
                now=CAPTURE_REFERENCE_NOW,
                raw_root=tmp_path,
                client=client,
                lock=NO_WRITER_LOCK,
                telemetry=PipelineTelemetry(
                    job=DeskLatencyClass.FAST.value, logger=telemetry_logger
                ),
            ),
        )
        await db_session.commit()

        await run_backbone_tick(
            db_session,
            TickContext.for_run(
                now=CAPTURE_REFERENCE_NOW,
                raw_root=tmp_path,
                lock=NO_WRITER_LOCK,
                telemetry=PipelineTelemetry(
                    job=DeskLatencyClass.BACKBONE.value, logger=telemetry_logger
                ),
            ),
        )
        await db_session.commit()

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "job=desk_fast" in msg and "step=scoreboard_ingest" in msg for msg in messages
    )
    assert any(
        "job=desk_backbone" in msg and "step=normalization" in msg for msg in messages
    )
    # The two classes never share a job label, so per-class timings cannot be
    # silently folded together.
    assert not any(
        "job=desk_fast" in msg and "step=normalization" in msg for msg in messages
    )


def test_every_latency_class_maps_to_a_distinct_pipeline_job() -> None:
    """Per-class freshness rows are what make independent failure observable.

    A shared row would let a healthy fast poller mask a projection class that
    has not run in hours -- the "rendered attractively instead of clearly
    stating that its data was stale" failure spec §4 names.
    """
    jobs = {cls: cls.pipeline_job for cls in DeskLatencyClass}
    assert len(set(jobs.values())) == len(DeskLatencyClass)
    assert jobs[DeskLatencyClass.FAST] == SummerLeaguePipelineJob.DESK_FAST
    assert jobs[DeskLatencyClass.PROJECTION] == SummerLeaguePipelineJob.DESK_PROJECTION
    assert jobs[DeskLatencyClass.BACKBONE] == SummerLeaguePipelineJob.DESK_BACKBONE
    # The composite keeps the original row so a rollback is a real rollback.
    assert jobs[DeskLatencyClass.COMPOSITE] == SummerLeaguePipelineJob.DESK
