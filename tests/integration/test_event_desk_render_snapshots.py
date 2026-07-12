"""Integration tests for `EventDeskRenderSnapshot` (launch-readiness item 9).

Covers what the unit codec tests (`tests/unit/test_event_desk_render_snapshots_codec.py`)
can't: table uniqueness, multi-variant coexistence/independent updates via the
repository's batch upsert, exact-key reads, and that the batch upsert issues a single
bounded SQL statement regardless of how many variants are written.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.schemas.event_desk import (
    Event,
    EventCalendarSource,
    EventDailyState,
    EventType,
)
from app.schemas.event_desk_render_snapshot import EventDeskRenderSnapshot
from app.services.event_desk.payload import (
    DeskFreshness,
    DeskHero,
    DeskLedgerRow,
    DeskPayload,
    DeskSlateRow,
    DeskTrackerSection,
)
from app.services.event_desk.render_snapshots import (
    CURRENT_SCHEMA_VERSION,
    RenderSnapshotWrite,
    deserialize_desk_view,
    get_render_snapshot,
    upsert_render_snapshots,
)
from app.services.summer_league.desk_read import DeskView
from tests.integration.perf._capture import count_queries

_N = {"i": 0}


def _make_event(**overrides: object) -> Event:
    _N["i"] += 1
    idx = _N["i"]
    defaults: dict[str, object] = dict(
        key=f"summer_league-render-snapshot-{idx}",
        name="Summer League",
        event_type=EventType.PRO_SUMMER,
        calendar_source=EventCalendarSource.SCHEDULE,
        calendar_ref={"competition_ids": [1]},
        window_priors={},
        cohort_basis="slot_window",
        priority=100,
        is_active=True,
    )
    defaults.update(overrides)
    return Event(**defaults)  # type: ignore[arg-type]


def _freshness(label: str = "as of 3:00pm ET") -> DeskFreshness:
    return DeskFreshness(
        last_tick_at=datetime(2026, 7, 10, 19, 0),
        next_tick_eta=datetime(2026, 7, 10, 20, 0),
        as_of_et_label=label,
    )


def _preview_view(headline: str = "Two lottery picks share the floor tonight.") -> DeskView:
    hero = DeskHero(
        kind="marquee",
        game_id=9,
        subject_player_id=101,
        subject_player_id_2=102,
        headline=headline,
        tagline=None,
        facts=[],
    )
    payload = DeskPayload(
        daily_state="preview",
        is_home_owner=True,
        hero=hero,
        slate=[
            DeskSlateRow(
                game_id=10,
                matchup_label="CHA vs ORL",
                status="scheduled",
                tip_datetime=datetime(2026, 7, 10, 23, 0),
                weight=60.0,
                read=None,
            ),
        ],
        live_board=[],
        ledger=[],
        tracker=DeskTrackerSection(cohort="lottery", stat_view="box", rows=[]),
        freshness=_freshness(),
    )
    return DeskView(
        payload=payload,
        players={101: {"display_name": "Prospect One", "slug": "prospect-one"}},
        matchups={},
        tracker_teams={},
    )


def _live_view() -> DeskView:
    payload = DeskPayload(
        daily_state="live",
        is_home_owner=True,
        hero=DeskHero(
            kind="live_duel",
            game_id=8,
            subject_player_id=101,
            subject_player_id_2=None,
            headline="Leads all rookies through three quarters.",
            tagline=None,
            facts=[],
        ),
        slate=[],
        live_board=[],
        ledger=[],
        tracker=DeskTrackerSection(cohort="round1", stat_view="per36", rows=[]),
        freshness=_freshness(),
    )
    return DeskView(payload=payload, players={}, matchups={}, tracker_teams={})


def _recap_view() -> DeskView:
    payload = DeskPayload(
        daily_state="recap",
        is_home_owner=True,
        hero=DeskHero(
            kind="performance_of_night",
            game_id=8,
            subject_player_id=101,
            subject_player_id_2=None,
            headline="Best debut by a #1 pick since 2019.",
            tagline=None,
            facts=[],
        ),
        slate=[],
        live_board=[],
        ledger=[
            DeskLedgerRow(
                game_id=8, player_id=101, gmsc=24.5, pctl=96.0, grade="hot", read=None
            ),
        ],
        tracker=DeskTrackerSection(cohort="undrafted", stat_view="advanced", rows=[]),
        freshness=_freshness(),
    )
    return DeskView(payload=payload, players={}, matchups={}, tracker_teams={})


@pytest.mark.asyncio
async def test_variant_uniqueness_rejects_duplicate_key(db_session: AsyncSession) -> None:
    """Two rows with the same (event, daily_state, cohort, stat_view) key violate uniqueness."""
    event = _make_event()
    db_session.add(event)
    await db_session.flush()
    assert event.id is not None

    row = EventDeskRenderSnapshot(
        event_id=event.id,
        daily_state=EventDailyState.PREVIEW,
        tracker_cohort="lottery",
        tracker_stat_view="box",
        schema_version=CURRENT_SCHEMA_VERSION,
        payload_json={"daily_state": "preview"},
        view_context_json={},
    )
    db_session.add(row)
    await db_session.flush()

    duplicate = EventDeskRenderSnapshot(
        event_id=event.id,
        daily_state=EventDailyState.PREVIEW,
        tracker_cohort="lottery",
        tracker_stat_view="box",
        schema_version=CURRENT_SCHEMA_VERSION,
        payload_json={"daily_state": "preview"},
        view_context_json={},
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_different_daily_state_is_a_distinct_row(db_session: AsyncSession) -> None:
    """The same event/cohort/stat_view but a different daily_state is a separate row."""
    event = _make_event()
    db_session.add(event)
    await db_session.flush()
    assert event.id is not None

    for state in (EventDailyState.PREVIEW, EventDailyState.LIVE, EventDailyState.RECAP):
        db_session.add(
            EventDeskRenderSnapshot(
                event_id=event.id,
                daily_state=state,
                tracker_cohort="lottery",
                tracker_stat_view="box",
                schema_version=CURRENT_SCHEMA_VERSION,
                payload_json={"daily_state": state.value},
                view_context_json={},
            )
        )
    await db_session.flush()

    result = await db_session.execute(
        select(EventDeskRenderSnapshot).where(
            EventDeskRenderSnapshot.event_id == event.id  # type: ignore[arg-type]
        )
    )
    rows = result.scalars().all()
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_upsert_and_get_round_trips_full_typed_desk_view(
    db_session: AsyncSession,
) -> None:
    """`upsert_render_snapshots` + `get_render_snapshot` round-trip a full DeskView."""
    event = _make_event()
    db_session.add(event)
    await db_session.flush()
    assert event.id is not None

    view = _preview_view()
    write = RenderSnapshotWrite(
        event_id=event.id,
        daily_state=EventDailyState.PREVIEW,
        tracker_cohort="lottery",
        tracker_stat_view="box",
        view=view,
        source_freshness_tick_at=datetime(2026, 7, 10, 19, 0),
        source_freshness_next_tick_eta=datetime(2026, 7, 10, 20, 0),
    )
    await upsert_render_snapshots(db_session, [write])
    await db_session.flush()

    row = await get_render_snapshot(
        db_session,
        event_id=event.id,
        daily_state=EventDailyState.PREVIEW,
        tracker_cohort="lottery",
        tracker_stat_view="box",
    )
    assert row is not None
    assert row.schema_version == CURRENT_SCHEMA_VERSION
    assert row.source_freshness_tick_at == datetime(2026, 7, 10, 19, 0)

    decoded = deserialize_desk_view(
        payload_json=row.payload_json,
        view_context_json=row.view_context_json,
        schema_version=row.schema_version,
    )
    assert decoded == view


@pytest.mark.asyncio
async def test_get_render_snapshot_returns_none_for_unmaterialized_variant(
    db_session: AsyncSession,
) -> None:
    """An exact read for a variant that was never upserted returns None, not an error."""
    event = _make_event()
    db_session.add(event)
    await db_session.flush()
    assert event.id is not None

    row = await get_render_snapshot(
        db_session,
        event_id=event.id,
        daily_state=EventDailyState.LIVE,
        tracker_cohort="round1",
        tracker_stat_view="per36",
    )
    assert row is None


@pytest.mark.asyncio
async def test_all_supported_variants_coexist_and_update_independently(
    db_session: AsyncSession,
) -> None:
    """Preview/Live/Recap rows for one event coexist; rewriting one leaves others untouched."""
    event = _make_event()
    db_session.add(event)
    await db_session.flush()
    assert event.id is not None

    writes = [
        RenderSnapshotWrite(
            event_id=event.id,
            daily_state=EventDailyState.PREVIEW,
            tracker_cohort="lottery",
            tracker_stat_view="box",
            view=_preview_view(headline="Original preview headline."),
        ),
        RenderSnapshotWrite(
            event_id=event.id,
            daily_state=EventDailyState.LIVE,
            tracker_cohort="lottery",
            tracker_stat_view="box",
            view=_live_view(),
        ),
        RenderSnapshotWrite(
            event_id=event.id,
            daily_state=EventDailyState.RECAP,
            tracker_cohort="lottery",
            tracker_stat_view="box",
            view=_recap_view(),
        ),
    ]
    await upsert_render_snapshots(db_session, writes)
    await db_session.flush()

    # Rewrite only the Preview variant with a changed headline.
    await upsert_render_snapshots(
        db_session,
        [
            RenderSnapshotWrite(
                event_id=event.id,
                daily_state=EventDailyState.PREVIEW,
                tracker_cohort="lottery",
                tracker_stat_view="box",
                view=_preview_view(headline="Updated preview headline."),
            )
        ],
    )
    await db_session.flush()

    preview_row = await get_render_snapshot(
        db_session,
        event_id=event.id,
        daily_state=EventDailyState.PREVIEW,
        tracker_cohort="lottery",
        tracker_stat_view="box",
    )
    live_row = await get_render_snapshot(
        db_session,
        event_id=event.id,
        daily_state=EventDailyState.LIVE,
        tracker_cohort="lottery",
        tracker_stat_view="box",
    )
    recap_row = await get_render_snapshot(
        db_session,
        event_id=event.id,
        daily_state=EventDailyState.RECAP,
        tracker_cohort="lottery",
        tracker_stat_view="box",
    )
    assert preview_row is not None and live_row is not None and recap_row is not None
    assert preview_row.payload_json is not None
    preview_hero = preview_row.payload_json["hero"]
    assert isinstance(preview_hero, dict)
    assert preview_hero["headline"] == "Updated preview headline."
    # The Live/Recap rows were untouched by the Preview-only rewrite.
    assert live_row.payload_json is not None
    assert live_row.payload_json["daily_state"] == "live"
    assert recap_row.payload_json is not None
    assert recap_row.payload_json["daily_state"] == "recap"

    result = await db_session.execute(
        select(EventDeskRenderSnapshot).where(
            EventDeskRenderSnapshot.event_id == event.id  # type: ignore[arg-type]
        )
    )
    assert len(result.scalars().all()) == 3


@pytest.mark.asyncio
async def test_upsert_render_snapshots_issues_one_bounded_statement(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """Batching N variant writes issues exactly one SQL statement, not N round trips."""
    event = _make_event()
    db_session.add(event)
    await db_session.flush()
    assert event.id is not None

    writes = [
        RenderSnapshotWrite(
            event_id=event.id,
            daily_state=EventDailyState.PREVIEW,
            tracker_cohort="lottery",
            tracker_stat_view="box",
            view=_preview_view(),
        ),
        RenderSnapshotWrite(
            event_id=event.id,
            daily_state=EventDailyState.LIVE,
            tracker_cohort="lottery",
            tracker_stat_view="box",
            view=_live_view(),
        ),
        RenderSnapshotWrite(
            event_id=event.id,
            daily_state=EventDailyState.RECAP,
            tracker_cohort="lottery",
            tracker_stat_view="box",
            view=_recap_view(),
        ),
    ]

    with count_queries(async_engine) as captured:
        await upsert_render_snapshots(db_session, writes)
        await db_session.flush()

    assert len(captured) == 1


@pytest.mark.asyncio
async def test_upsert_render_snapshots_is_a_noop_for_empty_writes(
    db_session: AsyncSession, async_engine: AsyncEngine
) -> None:
    """An empty write batch issues zero statements and never commits."""
    with count_queries(async_engine) as captured:
        await upsert_render_snapshots(db_session, [])

    assert captured == []


@pytest.mark.asyncio
async def test_upsert_render_snapshots_never_commits(db_session: AsyncSession) -> None:
    """The repository op flushes visibility within the session but leaves commit to the caller."""
    event = _make_event()
    db_session.add(event)
    await db_session.flush()
    assert event.id is not None

    await upsert_render_snapshots(
        db_session,
        [
            RenderSnapshotWrite(
                event_id=event.id,
                daily_state=EventDailyState.PREVIEW,
                tracker_cohort="lottery",
                tracker_stat_view="box",
                view=_preview_view(),
            )
        ],
    )
    # No explicit commit/flush call here -- `db_session.in_transaction()` should
    # still report an open, uncommitted transaction (the repository op did not
    # commit on the caller's behalf).
    assert db_session.in_transaction()
