"""Integration coverage for the flip-versus-compaction race of #766.

A rebuild stages its candidate outside the writer lock and compaction runs as its
own cron. Since Phase 3 the staged rows carry the event's own ``effective_day``,
which for a historical competition is already closed, so an overlapping rebuild
could make an in-flight candidate rank 2 and get it deleted before its own
pointer flip. The flip would then demote every current row for those scopes and
promote nothing, leaving the Explorer with no snapshot at all.

Both halves of the fix are exercised here, and both are asserted against the same
invariant: **no scope ends the sequence without a current row.**
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import SummerLeagueEdition
from app.schemas.summer_league_metrics import (
    SummerLeagueMetricContext,
    SummerLeagueDerivedAgg,
)
from app.services.summer_league.metric_compaction import compact_metric_versions
from app.services.summer_league.metric_publish import publish_metric_version
from app.services.summer_league.metric_publish_guards import (
    MetricCandidateVanishedError,
)
from tests.integration.conftest import make_player

# The event finished days ago, so every row it produces -- including a candidate
# staged right now -- lands on a closed event day.
CLOSED_EVENT_DAY = date(2026, 7, 10)
# Both rebuilds staged their candidates half an hour before the compaction run.
CANDIDATE_STAGED_AT = datetime(2026, 7, 20, 11, 30)
COMPACTION_NOW = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)


async def _seed_historical_competition(
    db: AsyncSession,
    *,
    league_id: str,
    published_version: int,
    candidate_versions: tuple[int, ...],
) -> tuple[int, int]:
    """Seed one published current version plus the given staged candidates.

    Args:
        db: Active session.
        league_id: Unique competition discriminator for this test.
        published_version: Version of the published, currently readable rows.
        candidate_versions: Versions staged unpublished, oldest first.

    Returns:
        The competition id and the player id used for the season grain.
    """
    competition = SummerLeagueEdition(
        year=2026,
        league_id=league_id,
        venue_slug="las_vegas",
        display_name=f"Race {league_id}",
    )
    player = make_player("Race", league_id.title())
    db.add_all([competition, player])
    await db.flush()
    assert competition.id is not None
    assert player.id is not None

    published_at = datetime(2026, 7, 11, 4)
    for version in (published_version, *candidate_versions):
        is_published = version == published_version
        for model, extra in (
            (SummerLeagueMetricContext, {}),
            (SummerLeagueDerivedAgg, {"player_id": player.id}),
        ):
            db.add(
                model(  # type: ignore[call-arg]
                    competition_id=competition.id,
                    year=2026,
                    venue_slug="las_vegas",
                    version=version,
                    is_current=is_published,
                    effective_day=CLOSED_EVENT_DAY,
                    published_at=published_at if is_published else None,
                    created_at=published_at if is_published else CANDIDATE_STAGED_AT,
                    **extra,
                )
            )
    await db.flush()
    return competition.id, player.id


async def _scopes_without_current_rows(db: AsyncSession) -> set[int]:
    """Return competition ids that hold projection rows but no current one."""
    orphaned: set[int] = set()
    for model in (SummerLeagueMetricContext, SummerLeagueDerivedAgg):
        rows = (
            await db.execute(
                select(model.competition_id, model.is_current)  # type: ignore[attr-defined]
            )
        ).all()
        seen: dict[int, bool] = {}
        for competition_id, is_current in rows:
            seen[int(competition_id)] = seen.get(int(competition_id), False) or bool(
                is_current
            )
        orphaned |= {
            competition_id
            for competition_id, has_current in seen.items()
            if not has_current
        }
    return orphaned


@pytest.mark.asyncio
async def test_flip_refuses_when_compaction_removed_the_candidate(
    db_session: AsyncSession,
) -> None:
    """A vanished candidate aborts the flip instead of emptying its scopes."""
    competition_id, _ = await _seed_historical_competition(
        db_session,
        league_id="race-vanished",
        published_version=1,
        candidate_versions=(10, 11),
    )
    await db_session.commit()

    # Compaction with the grace window disabled reproduces the pre-fix reaping:
    # rebuild A's v10 ranks 2 in the unpublished partition and is deleted.
    async with db_session.begin():
        summary = await compact_metric_versions(
            db_session,
            now=COMPACTION_NOW,
            candidate_grace_hours=0,
        )
    assert summary.context_rows_deleted == 1
    assert summary.season_rows_deleted == 1
    surviving = (
        (
            await db_session.execute(
                select(SummerLeagueMetricContext.version).where(
                    SummerLeagueMetricContext.competition_id == competition_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(surviving) == {1, 11}
    await db_session.commit()

    # Rebuild A now reaches its pointer flip with nothing left to promote.
    with pytest.raises(MetricCandidateVanishedError) as raised:
        async with db_session.begin():
            await publish_metric_version(db_session, version=10)
    assert str(competition_id) in str(raised.value)

    assert await _scopes_without_current_rows(db_session) == set()
    current_versions = (
        (
            await db_session.execute(
                select(SummerLeagueDerivedAgg.version).where(
                    SummerLeagueDerivedAgg.competition_id == competition_id,
                    SummerLeagueDerivedAgg.is_current.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    assert current_versions == [1]
    await db_session.commit()

    # The scope is not wedged: rebuild B's intact candidate still publishes.
    async with db_session.begin():
        assert await publish_metric_version(db_session, version=11) == set()
    assert await _scopes_without_current_rows(db_session) == set()
    published_current = (
        (
            await db_session.execute(
                select(SummerLeagueMetricContext.version).where(
                    SummerLeagueMetricContext.competition_id == competition_id,
                    SummerLeagueMetricContext.is_current.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    assert published_current == [11]


@pytest.mark.asyncio
async def test_compaction_spares_an_in_flight_candidate(
    db_session: AsyncSession,
) -> None:
    """The grace window keeps the race from arising: A's candidate survives B's."""
    competition_id, _ = await _seed_historical_competition(
        db_session,
        league_id="race-inflight",
        published_version=1,
        candidate_versions=(10, 11),
    )
    await db_session.commit()

    # Both candidates were staged half an hour ago, so the compaction cron that
    # fires between staging and the flip must leave them alone.
    async with db_session.begin():
        summary = await compact_metric_versions(db_session, now=COMPACTION_NOW)
    assert summary.rows_deleted == 0
    await db_session.commit()

    # Rebuild A therefore finds its candidate intact and flips normally.
    async with db_session.begin():
        assert await publish_metric_version(db_session, version=10) == set()

    assert await _scopes_without_current_rows(db_session) == set()
    current_versions = (
        (
            await db_session.execute(
                select(SummerLeagueDerivedAgg.version).where(
                    SummerLeagueDerivedAgg.competition_id == competition_id,
                    SummerLeagueDerivedAgg.is_current.is_(True),  # type: ignore[attr-defined]
                )
            )
        )
        .scalars()
        .all()
    )
    assert current_versions == [10]


@pytest.mark.asyncio
async def test_grace_window_still_compacts_older_abandoned_candidates(
    db_session: AsyncSession,
) -> None:
    """The exemption is a window, not an amnesty: aged candidates still compact."""
    competition_id, _ = await _seed_historical_competition(
        db_session,
        league_id="race-abandoned",
        published_version=1,
        candidate_versions=(10, 11),
    )
    await db_session.commit()

    # Long past any plausible rebuild, so both candidates are genuinely abandoned
    # and the documented one-per-scope-per-day bound applies again.
    async with db_session.begin():
        summary = await compact_metric_versions(
            db_session,
            now=CANDIDATE_STAGED_AT.replace(tzinfo=timezone.utc) + timedelta(hours=12),
        )

    assert summary.context_rows_deleted == 1
    assert summary.season_rows_deleted == 1
    remaining = (
        (
            await db_session.execute(
                select(SummerLeagueMetricContext.version).where(
                    SummerLeagueMetricContext.competition_id == competition_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(remaining) == {1, 11}
    assert await _scopes_without_current_rows(db_session) == set()
