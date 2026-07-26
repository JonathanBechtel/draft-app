"""Integration tests for the Competition Context refresh orchestration (#618).

`app.services.summer_league.environment_refresh` wires the frozen #617
aggregation contract into the production pipeline. These tests drive it
against a real Postgres session:

* a pipeline-shaped incremental refresh publishes a competition's profile
  plus its season profile, and records durable pipeline state;
* a failure inside the aggregation call is isolated (never raised) and
  preserves the last good, previously published profile;
* recovery: a subsequent successful refresh clears a prior recorded failure;
* rollback restores an already-published prior version to current, and is a
  no-op when the target is already current;
* a two-session, barrier-synchronized concurrency test proves the refresh
  and the rollback path both participate in the same shared Summer League
  writer lock, in both acquisition orders -- neither can publish while the
  other holds it.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.summer_league_environment import SummerLeagueEnvironmentProfile
from app.schemas.summer_league_pipeline import (
    SummerLeaguePipelineJob,
    SummerLeaguePipelineOutcome,
    SummerLeaguePipelineState,
)
from app.services.summer_league.environment_refresh import (
    EnvironmentRollbackResult,
    refresh_environment_profiles_for_year,
    rollback_environment_profile,
)
from app.services.summer_league.write_lock import acquire_summer_league_writer_lock
from app.services.summer_league_environment_service import (
    EnvironmentScope,
    get_environment_profile,
)
from tests.integration.test_summer_league_environment_profiles import (
    _seed_competition,
)

pytestmark = pytest.mark.asyncio


async def _pipeline_state(
    db: AsyncSession,
    job: SummerLeaguePipelineJob = SummerLeaguePipelineJob.ENVIRONMENT_REFRESH,
) -> SummerLeaguePipelineState | None:
    return (
        await db.execute(
            select(SummerLeaguePipelineState).where(
                SummerLeaguePipelineState.job == job
            )
        )
    ).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Pipeline ordering: one competition update plus its season profile
# --------------------------------------------------------------------------- #
async def test_refresh_publishes_competition_and_season_profile(
    db_session: AsyncSession,
) -> None:
    """A pipeline-shaped call publishes the year's season + competition scopes.

    Mirrors the ingest runner's call site: already inside an open, locked
    transaction (the runner's ``metrics_and_snapshots`` phase).
    """
    comp_id, _ = await _seed_competition(
        db_session, year=2025, venue="las_vegas", league_id="15"
    )
    await db_session.commit()

    async with db_session.begin():
        await acquire_summer_league_writer_lock(db_session)
        outcome = await refresh_environment_profiles_for_year(db_session, year=2025)

    assert outcome.succeeded is True
    assert outcome.built_scopes == 2  # season + one competition
    assert outcome.failures == {}

    season = await get_environment_profile(
        db_session, EnvironmentScope.for_season(2025)
    )
    competition = await get_environment_profile(
        db_session, EnvironmentScope.for_competition(comp_id, 2025)
    )
    assert season is not None
    assert competition is not None

    state = await _pipeline_state(db_session)
    assert state is not None
    assert state.last_outcome == SummerLeaguePipelineOutcome.SUCCEEDED
    assert state.last_succeeded_at is not None
    assert state.last_failure_reason is None


# --------------------------------------------------------------------------- #
# Failure isolation: last-good preserved, never corrupts normalized facts
# --------------------------------------------------------------------------- #
async def test_refresh_failure_preserves_last_good_and_is_recorded(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raised aggregation error is isolated: last-good profile stays published."""
    comp_id, _ = await _seed_competition(
        db_session, year=2024, venue="las_vegas", league_id="15"
    )
    await db_session.commit()

    async with db_session.begin():
        await acquire_summer_league_writer_lock(db_session)
        good_outcome = await refresh_environment_profiles_for_year(
            db_session, year=2024
        )
    assert good_outcome.succeeded is True

    good = await get_environment_profile(
        db_session, EnvironmentScope.for_competition(comp_id, 2024)
    )
    assert good is not None
    good_version = good.version
    await db_session.commit()

    import app.services.summer_league.environment_refresh as refresh_mod

    async def _boom(db: object, *, year: int) -> None:
        raise RuntimeError("forced aggregation failure")

    monkeypatch.setattr(refresh_mod, "rebuild_environment_profiles", _boom)

    async with db_session.begin():
        await acquire_summer_league_writer_lock(db_session)
        bad_outcome = await refresh_mod.refresh_environment_profiles_for_year(
            db_session, year=2024
        )

    assert bad_outcome.succeeded is False
    assert bad_outcome.error == "RuntimeError: forced aggregation failure"

    still = await get_environment_profile(
        db_session, EnvironmentScope.for_competition(comp_id, 2024)
    )
    assert still is not None
    assert still.version == good_version  # unchanged; last-good preserved

    state = await _pipeline_state(db_session)
    assert state is not None
    assert state.last_outcome == SummerLeaguePipelineOutcome.FAILED
    assert state.last_failure_reason == "RuntimeError: forced aggregation failure"


# --------------------------------------------------------------------------- #
# Recovery: a subsequent successful run clears the recorded failure
# --------------------------------------------------------------------------- #
async def test_recovery_after_failure_clears_failure_state(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later successful refresh clears the prior failure's durable state."""
    await _seed_competition(db_session, year=2023, venue="las_vegas", league_id="15")
    await db_session.commit()

    import app.services.summer_league.environment_refresh as refresh_mod

    async def _boom(db: object, *, year: int) -> None:
        raise RuntimeError("transient failure")

    monkeypatch.setattr(refresh_mod, "rebuild_environment_profiles", _boom)
    async with db_session.begin():
        await acquire_summer_league_writer_lock(db_session)
        failed_outcome = await refresh_mod.refresh_environment_profiles_for_year(
            db_session, year=2023
        )
    assert failed_outcome.succeeded is False

    state = await _pipeline_state(db_session)
    assert state is not None
    assert state.last_outcome == SummerLeaguePipelineOutcome.FAILED
    await db_session.commit()  # end the read txn before the next begin block

    monkeypatch.undo()  # restore the real rebuild_environment_profiles
    async with db_session.begin():
        await acquire_summer_league_writer_lock(db_session)
        recovered_outcome = await refresh_environment_profiles_for_year(
            db_session, year=2023
        )
    assert recovered_outcome.succeeded is True

    state = await _pipeline_state(db_session)
    assert state is not None
    assert state.last_outcome == SummerLeaguePipelineOutcome.SUCCEEDED
    assert state.last_failure_reason is None


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #
async def test_rollback_restores_prior_version(db_session: AsyncSession) -> None:
    """Rolling back to version 1 demotes version 2 and promotes version 1."""
    comp_id, _ = await _seed_competition(
        db_session, year=2022, venue="las_vegas", league_id="15"
    )
    await db_session.commit()

    async with db_session.begin():
        await acquire_summer_league_writer_lock(db_session)
        await refresh_environment_profiles_for_year(db_session, year=2022)
    async with db_session.begin():
        await acquire_summer_league_writer_lock(db_session)
        await refresh_environment_profiles_for_year(db_session, year=2022)

    scope_key = f"competition:{comp_id}"
    rows = (
        await db_session.execute(
            select(
                SummerLeagueEnvironmentProfile.version,
                SummerLeagueEnvironmentProfile.is_current,
            )
            .where(SummerLeagueEnvironmentProfile.scope_key == scope_key)
            .order_by(SummerLeagueEnvironmentProfile.version)
        )
    ).all()
    assert [v for v, _ in rows] == [1, 2]
    assert [c for _, c in rows] == [False, True]
    await db_session.commit()  # end the read txn before the next begin block

    async with db_session.begin():
        result = await rollback_environment_profile(
            db_session, scope_key=scope_key, target_version=1
        )
    assert result.changed is True
    assert result.previous_current_version == 2
    assert result.restored_version == 1

    rows = (
        await db_session.execute(
            select(
                SummerLeagueEnvironmentProfile.version,
                SummerLeagueEnvironmentProfile.is_current,
            )
            .where(SummerLeagueEnvironmentProfile.scope_key == scope_key)
            .order_by(SummerLeagueEnvironmentProfile.version)
        )
    ).all()
    assert [v for v, _ in rows] == [1, 2]
    assert [c for _, c in rows] == [True, False]

    current = await get_environment_profile(
        db_session, EnvironmentScope.for_competition(comp_id, 2022)
    )
    assert current is not None and current.version == 1


async def test_rollback_to_already_current_version_is_a_noop(
    db_session: AsyncSession,
) -> None:
    """Rolling back to the already-current version changes nothing."""
    comp_id, _ = await _seed_competition(
        db_session, year=2021, venue="las_vegas", league_id="15"
    )
    await db_session.commit()

    async with db_session.begin():
        await acquire_summer_league_writer_lock(db_session)
        await refresh_environment_profiles_for_year(db_session, year=2021)

    scope_key = f"competition:{comp_id}"
    async with db_session.begin():
        result = await rollback_environment_profile(
            db_session, scope_key=scope_key, target_version=1
        )
    assert result.changed is False
    assert result.previous_current_version == 1
    assert result.restored_version == 1


async def test_rollback_unknown_version_raises(db_session: AsyncSession) -> None:
    """An unknown target version raises rather than silently no-op-ing."""
    comp_id, _ = await _seed_competition(
        db_session, year=2020, venue="las_vegas", league_id="15"
    )
    await db_session.commit()

    async with db_session.begin():
        await acquire_summer_league_writer_lock(db_session)
        await refresh_environment_profiles_for_year(db_session, year=2020)

    scope_key = f"competition:{comp_id}"
    with pytest.raises(ValueError, match="no version 99"):
        async with db_session.begin():
            await rollback_environment_profile(
                db_session, scope_key=scope_key, target_version=99
            )


async def test_rollback_unknown_scope_key_raises(db_session: AsyncSession) -> None:
    """A scope_key with no published versions at all raises, never no-ops."""
    with pytest.raises(ValueError, match="no profile versions exist"):
        async with db_session.begin():
            await rollback_environment_profile(
                db_session, scope_key="season:1999", target_version=1
            )


# --------------------------------------------------------------------------- #
# Two-session concurrency (barrier-synchronized), both lock-acquisition orders
# --------------------------------------------------------------------------- #
@pytest.mark.committed_db
async def test_concurrent_refresh_blocks_rollback(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """A pipeline refresh holding the lock serializes a concurrent rollback attempt."""
    comp_id, _ = await _seed_competition(
        db_session, year=2025, venue="salt_lake_city", league_id="13"
    )
    await db_session.commit()
    async with db_session.begin():
        await acquire_summer_league_writer_lock(db_session)
        await refresh_environment_profiles_for_year(db_session, year=2025)

    scope_key = f"competition:{comp_id}"
    b_reached_lock = asyncio.Event()
    b_acquired_lock = asyncio.Event()
    rollback_result: dict[str, EnvironmentRollbackResult] = {}

    async def competing_rollback() -> None:
        async with session_factory() as db_b:
            await db_b.execute(text(f'SET search_path TO "{test_schema}"'))
            await db_b.commit()
            async with db_b.begin():
                b_reached_lock.set()
                # Blocks until session A's transaction releases the lock.
                result = await rollback_environment_profile(
                    db_b, scope_key=scope_key, target_version=1
                )
                b_acquired_lock.set()
                rollback_result["value"] = result

    async with session_factory() as db_a:
        await db_a.execute(text(f'SET search_path TO "{test_schema}"'))
        await db_a.commit()
        async with db_a.begin():
            await acquire_summer_league_writer_lock(db_a)
            task = asyncio.create_task(competing_rollback())
            await asyncio.wait_for(b_reached_lock.wait(), timeout=5.0)
            await asyncio.sleep(0.3)
            assert not b_acquired_lock.is_set(), "rollback was not serialized"

            # A performs a refresh (pipeline-shaped call) while still holding
            # the lock, publishing version 2.
            outcome = await refresh_environment_profiles_for_year(db_a, year=2025)
            assert outcome.succeeded is True
        # Commit releases the lock, unblocking B.
        await asyncio.wait_for(task, timeout=10.0)

    assert b_acquired_lock.is_set()
    # B's rollback ran only after A's version-2 publish committed (READ
    # COMMITTED sees it fresh post-unblock), so target_version=1 is now a
    # real change -- proving both serialization *and* correct snapshot
    # visibility across the handoff, not just mutual exclusion.
    result = rollback_result["value"]
    assert result.changed is True
    assert result.previous_current_version == 2
    assert result.restored_version == 1

    current = await get_environment_profile(
        db_session, EnvironmentScope.for_competition(comp_id, 2025)
    )
    assert current is not None and current.version == 1


@pytest.mark.committed_db
async def test_concurrent_rollback_blocks_refresh(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
) -> None:
    """A standalone rollback holding the lock serializes a concurrent pipeline refresh.

    The reverse acquisition order from `test_concurrent_refresh_blocks_rollback`
    -- proves the two entry points share one lock regardless of which side
    gets there first.
    """
    comp_id, _ = await _seed_competition(
        db_session, year=2025, venue="california_classic", league_id="16"
    )
    await db_session.commit()
    async with db_session.begin():
        await acquire_summer_league_writer_lock(db_session)
        await refresh_environment_profiles_for_year(db_session, year=2025)

    b_reached_lock = asyncio.Event()
    b_acquired_lock = asyncio.Event()

    async def competing_refresh() -> None:
        async with session_factory() as db_b:
            await db_b.execute(text(f'SET search_path TO "{test_schema}"'))
            await db_b.commit()
            async with db_b.begin():
                b_reached_lock.set()
                outcome = await refresh_environment_profiles_for_year(db_b, year=2025)
                b_acquired_lock.set()
                assert outcome.succeeded is True

    scope_key = f"competition:{comp_id}"
    async with session_factory() as db_a:
        await db_a.execute(text(f'SET search_path TO "{test_schema}"'))
        await db_a.commit()
        async with db_a.begin():
            # A acquires the lock via the rollback entrypoint this time.
            result = await rollback_environment_profile(
                db_a, scope_key=scope_key, target_version=1
            )
            assert result.changed is False
            task = asyncio.create_task(competing_refresh())
            await asyncio.wait_for(b_reached_lock.wait(), timeout=5.0)
            await asyncio.sleep(0.3)
            assert not b_acquired_lock.is_set(), "refresh was not serialized"
        await asyncio.wait_for(task, timeout=10.0)

    assert b_acquired_lock.is_set()
