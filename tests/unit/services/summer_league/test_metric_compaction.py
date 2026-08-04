"""Unit coverage for Summer League metric-version compaction helpers."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cli import summer_league_metrics_compact
from app.schemas.summer_league_metrics import SummerLeagueMetricContext
from app.services.sources.summer_league import metric_compaction
from app.services.ingest.write_lock import SummerLeagueWriterLockTimeout


def test_closed_day_cutoff_normalizes_aware_clock_to_utc() -> None:
    """Compaction closes days at UTC midnight, regardless of caller timezone."""
    cutoff = metric_compaction._closed_day_cutoff(
        datetime(2026, 7, 7, 1, tzinfo=timezone.utc)
    )

    assert cutoff == datetime(2026, 7, 7)
    assert metric_compaction._closed_day_cutoff(datetime(2026, 7, 7, 1)) == cutoff


@pytest.mark.asyncio
async def test_delete_helper_returns_database_rowcount() -> None:
    """The scoped delete helper reports the database's affected-row count."""
    db = AsyncMock()
    db.execute.return_value = type("Result", (), {"rowcount": 4})()

    deleted = await metric_compaction._delete_superseded_closed_day_rows(
        db,
        model=SummerLeagueMetricContext,
        scope_columns=(SummerLeagueMetricContext.competition_id,),
        event_day_cutoff=date(2026, 7, 7),
    )

    assert deleted == 4
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_compaction_acquires_lock_and_compacts_both_projection_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One run serializes and compacts contexts plus player-season rows."""
    lock = AsyncMock()
    delete_rows = AsyncMock(side_effect=[2, 5])
    monkeypatch.setattr(
        metric_compaction, "acquire_summer_league_writer_lock_bounded", lock
    )
    monkeypatch.setattr(
        metric_compaction,
        "_delete_superseded_closed_day_rows",
        delete_rows,
    )

    summary = await metric_compaction.compact_metric_versions(
        AsyncMock(),
        now=datetime(2026, 7, 8, 4, tzinfo=timezone.utc),
    )

    assert summary.context_rows_deleted == 2
    assert summary.season_rows_deleted == 5
    lock.assert_awaited_once()
    assert delete_rows.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("grace_hours", "expected_cutoff"),
    [
        (6.0, datetime(2026, 7, 7, 22)),
        (0, None),
    ],
)
async def test_compaction_derives_the_in_flight_candidate_grace_cutoff(
    monkeypatch: pytest.MonkeyPatch,
    grace_hours: float,
    expected_cutoff: datetime | None,
) -> None:
    """The grace window becomes a naive-UTC birth cutoff, or None when disabled."""
    delete_rows = AsyncMock(side_effect=[0, 0])
    monkeypatch.setattr(
        metric_compaction,
        "acquire_summer_league_writer_lock_bounded",
        AsyncMock(),
    )
    monkeypatch.setattr(
        metric_compaction, "_delete_superseded_closed_day_rows", delete_rows
    )

    await metric_compaction.compact_metric_versions(
        AsyncMock(),
        now=datetime(2026, 7, 8, 4, tzinfo=timezone.utc),
        candidate_grace_hours=grace_hours,
    )

    assert all(
        call.kwargs["candidate_grace_cutoff"] == expected_cutoff
        for call in delete_rows.await_args_list
    )


@pytest.mark.asyncio
async def test_compaction_uses_the_configured_bounded_lock_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compaction forwards its explicit wait bound to the shared lock helper."""
    db = AsyncMock()
    lock = AsyncMock()
    delete_rows = AsyncMock(side_effect=[0, 0])
    monkeypatch.setattr(
        metric_compaction, "acquire_summer_league_writer_lock_bounded", lock
    )
    monkeypatch.setattr(
        metric_compaction, "_delete_superseded_closed_day_rows", delete_rows
    )
    await metric_compaction.compact_metric_versions(
        db,
        now=datetime(2026, 7, 8, tzinfo=timezone.utc),
        max_wait_seconds=2.5,
    )
    lock.assert_awaited_once_with(db, max_wait_seconds=2.5)


@pytest.mark.asyncio
async def test_cli_runs_compaction_in_a_transaction_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The shipped CLI owns the transaction and always disposes its engine."""
    database = AsyncMock()
    session_context = AsyncMock()
    session_context.__aenter__.return_value = database
    transaction_context = AsyncMock()
    database.begin = MagicMock(return_value=transaction_context)
    summary = metric_compaction.MetricCompactionSummary(
        cutoff=datetime(2026, 7, 7),
        context_rows_deleted=2,
        season_rows_deleted=3,
    )
    compact = AsyncMock(return_value=summary)
    dispose = AsyncMock()
    monkeypatch.setattr(
        summer_league_metrics_compact, "SessionLocal", lambda: session_context
    )
    monkeypatch.setattr(
        summer_league_metrics_compact,
        "compact_metric_versions",
        compact,
    )
    monkeypatch.setattr(
        summer_league_metrics_compact,
        "engine",
        SimpleNamespace(dispose=dispose),
    )

    await summer_league_metrics_compact.main()

    compact.assert_awaited_once_with(database)
    dispose.assert_awaited_once()
    assert "2 contexts, 3 player-seasons" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_reports_a_lock_contended_run_as_a_clean_skip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A busy writer lock exits successfully and tells cron to retry tomorrow."""
    database = AsyncMock()
    session_context = AsyncMock()
    session_context.__aenter__.return_value = database
    transaction_context = AsyncMock()
    database.begin = MagicMock(return_value=transaction_context)
    compact = AsyncMock(
        side_effect=SummerLeagueWriterLockTimeout("Timed out after 30.0s")
    )
    dispose = AsyncMock()
    monkeypatch.setattr(
        summer_league_metrics_compact, "SessionLocal", lambda: session_context
    )
    monkeypatch.setattr(
        summer_league_metrics_compact, "compact_metric_versions", compact
    )
    monkeypatch.setattr(
        summer_league_metrics_compact,
        "engine",
        SimpleNamespace(dispose=dispose),
    )

    await summer_league_metrics_compact.main()

    output = capsys.readouterr().out
    assert "Skipped Summer League metric-version compaction" in output
    assert "will retry tomorrow" in output
    dispose.assert_awaited_once()
