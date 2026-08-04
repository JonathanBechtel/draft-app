"""Unit coverage for the backbone's normalization transaction boundary."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.schemas.event_desk import EventDailyState
from app.schemas.summer_league import SummerLeagueEdition
from app.services.summer_league.desk_tick import backbone
from app.services.summer_league.desk_tick.shared import NO_WRITER_LOCK, TickContext


@pytest.mark.asyncio
async def test_backbone_releases_normalization_rows_before_metrics_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalization commits before the long rebuild, then the caller's lock returns."""
    calls: list[str] = []
    competition = SummerLeagueEdition(year=2026, league_id="15")
    competition.id = 7

    async def fake_normalize(*args: object, **kwargs: object) -> bool:
        calls.append("normalize")
        return True

    async def fake_rebuild(*args: object, **kwargs: object) -> None:
        calls.append("rebuild")

    async def release_transaction() -> None:
        calls.append("commit")

    monkeypatch.setattr(backbone, "normalize_competition", fake_normalize)
    monkeypatch.setattr(backbone, "rebuild_sl_metrics", fake_rebuild)

    result = await backbone.run_backbone_tick(
        AsyncMock(),
        TickContext(
            now=datetime(2026, 7, 10, 19, 30),
            transaction_boundary=release_transaction,
            lock=NO_WRITER_LOCK,
        ),
        competitions=(competition,),
        daily_state=EventDailyState.LIVE,
    )

    assert result.metrics_rebuilt is True
    assert calls == ["normalize", "commit", "rebuild"]
