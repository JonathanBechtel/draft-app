"""Stable Summer League metrics-input watermark behavior."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.summer_league import metrics_input
from app.services.summer_league.metrics_input import (
    calculate_metrics_input_watermark,
)


class _FakeResult:
    """Prepared ordered rows for one watermark input relation."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        """Return prepared rows."""
        return self._rows


class _FakeDb:
    """Return the six prepared watermark relations in query order."""

    def __init__(self, relations: list[list[tuple[Any, ...]]]) -> None:
        self._relations = list(relations)

    async def execute(self, statement: object) -> _FakeResult:
        """Return the next relation without evaluating its SQL expression."""
        del statement
        return _FakeResult(self._relations.pop(0))


def _relations(
    *,
    raw_sha: str = "raw-sha",
    player_id: int | None = 7,
    game_date: str = "2026-07-10",
    player_game_log_hash: str = "player-game-log-hash",
    shot_event_hash: str = "shot-event-hash",
) -> list[list[tuple[Any, ...]]]:
    """Build a minimal complete set of stable metrics input rows."""
    return [
        [("2026/15/game.json", raw_sha, "PARSED", 25)],
        [("1640001", player_id, "EXACT")],
        [
            (
                "1522600001",
                "final",
                game_date,
                "2026-07-10T22:00:00",
                10,
                11,
                88,
                84,
            )
        ],
        [(1, 2026, "las_vegas")],
        [(1, player_game_log_hash)],
        [(1, shot_event_hash)],
    ]


@pytest.mark.asyncio
async def test_metrics_input_watermark_is_stable_for_unchanged_content() -> None:
    """Two identical input snapshots produce exactly the same watermark."""
    first = await calculate_metrics_input_watermark(_FakeDb(_relations()))  # type: ignore[arg-type]
    second = await calculate_metrics_input_watermark(_FakeDb(_relations()))  # type: ignore[arg-type]

    assert first == second
    assert len(first) == 64


@pytest.mark.asyncio
async def test_metrics_input_watermark_advances_for_raw_or_identity_changes() -> None:
    """Any source, normalized log, or identity change invalidates metrics."""
    baseline = await calculate_metrics_input_watermark(_FakeDb(_relations()))  # type: ignore[arg-type]
    raw_changed = await calculate_metrics_input_watermark(  # type: ignore[arg-type]
        _FakeDb(_relations(raw_sha="new-sha"))
    )
    identity_changed = await calculate_metrics_input_watermark(  # type: ignore[arg-type]
        _FakeDb(_relations(player_id=8))
    )
    schedule_changed = await calculate_metrics_input_watermark(  # type: ignore[arg-type]
        _FakeDb(_relations(game_date="2026-07-11"))
    )
    game_log_changed = await calculate_metrics_input_watermark(  # type: ignore[arg-type]
        _FakeDb(_relations(player_game_log_hash="changed-player-game-log-hash"))
    )
    shot_event_changed = await calculate_metrics_input_watermark(  # type: ignore[arg-type]
        _FakeDb(_relations(shot_event_hash="changed-shot-event-hash"))
    )

    assert raw_changed != baseline
    assert identity_changed != baseline
    assert schedule_changed != baseline
    assert game_log_changed != baseline
    assert shot_event_changed != baseline


@pytest.mark.asyncio
async def test_out_of_band_game_log_mutation_changes_gate_watermark() -> None:
    """A repaired normalized game-log row makes the next gate rebuild."""
    before_repair = await calculate_metrics_input_watermark(  # type: ignore[arg-type]
        _FakeDb(_relations(player_game_log_hash="before-repair"))
    )
    after_repair = await calculate_metrics_input_watermark(  # type: ignore[arg-type]
        _FakeDb(_relations(player_game_log_hash="after-repair"))
    )

    # The rebuild gate compares these durable fingerprints; a changed value
    # must never be treated as an unchanged-input skip.
    assert after_repair != before_repair


@pytest.mark.asyncio
async def test_metrics_implementation_change_invalidates_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing the metrics implementation fingerprint forces a rebuild."""
    baseline = await calculate_metrics_input_watermark(_FakeDb(_relations()))  # type: ignore[arg-type]
    monkeypatch.setattr(
        metrics_input,
        "METRICS_IMPLEMENTATION_FINGERPRINT",
        "changed-metrics-implementation",
    )
    changed = await calculate_metrics_input_watermark(_FakeDb(_relations()))  # type: ignore[arg-type]

    assert changed != baseline
