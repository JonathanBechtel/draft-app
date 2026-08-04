"""Unit tests for the Summer League metric publication seams."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.sources.summer_league import metric_publish


def _empty_projection_result(newer_competition_ids: list[int]) -> SimpleNamespace:
    """Return a result usable by every read the publisher issues before the flip.

    ``scalars().all()`` feeds the newer-version guard; the bare ``all()`` feeds the
    candidate-presence guard, whose empty grouping means "no scope holds current
    rows", so nothing is demoted and nothing can have vanished.
    """
    return SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: newer_competition_ids),
        all=lambda: [],
    )


def _fit_result() -> SimpleNamespace:
    """Return the small fit-shaped object consumed by the publisher."""
    return SimpleNamespace(
        pyth_exponent=1.2,
        ws_ppw_coeff=3.3,
        pyth_n=12,
        bpm_intercept=0.1,
        bpm_r2=0.8,
        bpm_n_fit=20,
        bpm_coef={"pts": 0.4},
    )


@pytest.mark.asyncio
async def test_publish_metric_model_stages_a_new_inactive_fit() -> None:
    """Staging records a candidate without disturbing the active fit."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)

    await metric_publish.publish_metric_model(
        db,
        version="candidate",
        result=_fit_result(),  # type: ignore[arg-type]
        activate=False,
    )

    model = db.add.call_args.args[0]
    assert model.model_version == "candidate"
    assert model.is_active is False
    assert model.bpm_coefficients == {"pts": 0.4}
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_publish_metric_model_refits_and_activates_existing_fit() -> None:
    """A colliding version is updated in place and can become active."""
    db = MagicMock()
    db.execute = AsyncMock()
    existing = SimpleNamespace(is_active=False)
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: existing)

    await metric_publish.publish_metric_model(
        db,
        version="candidate",
        result=_fit_result(),  # type: ignore[arg-type]
    )

    assert existing.is_active is True
    assert existing.pyth_exponent == 1.2
    assert existing.bpm_coefficients == {"pts": 0.4}
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_next_metric_version_uses_the_highest_projection_version() -> None:
    """The next publication sequence spans context and player projections."""
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[None, 4, 9])

    assert await metric_publish.next_metric_version(db) == 10


@pytest.mark.asyncio
async def test_next_metric_version_uses_the_database_sequence() -> None:
    """Production publication versions come from the atomic database sequence."""
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=["summer_league_metric_version_seq", 12])

    assert await metric_publish.next_metric_version(db) == 12


def _update_shapes(db: MagicMock) -> list[tuple[str, frozenset[str]]]:
    """Return ``(table, assigned columns)`` for each UPDATE the publisher issued.

    Reads statements in await order, so the demote-then-promote sequence the
    partial unique indexes depend on is visible to assertions.
    """
    return [
        (
            statement.table.name,
            frozenset(str(key).rsplit(".", 1)[-1] for key in statement._values),
        )
        for statement in (call.args[0] for call in db.execute.await_args_list)
        if hasattr(statement, "_values")
    ]


@pytest.mark.asyncio
async def test_publish_metric_version_flips_full_projection_and_fit() -> None:
    """A full publication demotes old rows before promoting the candidate."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.execute.return_value = _empty_projection_result([])

    await metric_publish.publish_metric_version(
        db, version=9, model_version="candidate"
    )

    # One flush separates the demotions from the promotions; the partial unique
    # indexes reject the candidate otherwise.
    assert db.flush.await_count == 1
    assert _update_shapes(db) == [
        ("summer_league_player_seasons", frozenset({"is_current"})),
        ("summer_league_metric_contexts", frozenset({"is_current"})),
        ("summer_league_player_seasons", frozenset({"is_current", "published_at"})),
        ("summer_league_metric_contexts", frozenset({"is_current", "published_at"})),
        # A full publication also deactivates every prior fit and activates the
        # candidate's own model version.
        ("summer_league_metric_models", frozenset({"is_active"})),
        ("summer_league_metric_models", frozenset({"is_active"})),
    ]


@pytest.mark.asyncio
async def test_publish_metric_version_stamps_input_watermark_on_promoted_rows() -> None:
    """The source watermark is written at the pointer flip, not left on candidates."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.execute.return_value = _empty_projection_result([])
    watermark = datetime(2026, 7, 28, 12, 0)

    await metric_publish.publish_metric_version(
        db,
        version=9,
        model_version="candidate",
        as_of=watermark,
        effective_day=date(2026, 7, 28),
    )

    statements = [call.args[0] for call in db.execute.await_args_list]
    assert "as_of" in str(statements[5])
    assert "as_of" in str(statements[6])
    assert "effective_day" in str(statements[5])
    assert "effective_day" in str(statements[6])


@pytest.mark.asyncio
async def test_publish_metric_version_scopes_the_pointer_flip() -> None:
    """A scoped publication leaves the league-wide fit untouched."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.execute.return_value = _empty_projection_result([])

    await metric_publish.publish_metric_version(db, version=10, competition_ids={3, 5})

    assert db.flush.await_count == 1
    shapes = _update_shapes(db)
    # Only the two projections flip: the league-wide fit table is never updated.
    assert shapes == [
        ("summer_league_player_seasons", frozenset({"is_current"})),
        ("summer_league_metric_contexts", frozenset({"is_current"})),
        ("summer_league_player_seasons", frozenset({"is_current", "published_at"})),
        ("summer_league_metric_contexts", frozenset({"is_current", "published_at"})),
    ]
    updates = [
        statement
        for statement in (call.args[0] for call in db.execute.await_args_list)
        if hasattr(statement, "_values")
    ]
    assert all("competition_id IN" in str(statement) for statement in updates)


@pytest.mark.asyncio
async def test_publish_metric_version_skips_newer_current_scope() -> None:
    """A stale candidate excludes newer scopes from both projection updates."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.execute.return_value = _empty_projection_result([3])

    await metric_publish.publish_metric_version(db, version=9, competition_ids={3, 5})

    assert db.flush.await_count == 1
    assert db.execute.await_count == 7
    statements = [call.args[0] for call in db.execute.await_args_list]
    assert "published_at" not in str(statements[3])
    assert "published_at" not in str(statements[4])
    assert "published_at" in str(statements[5])
    assert "published_at" in str(statements[6])
    assert all("NOT IN" in str(statement) for statement in statements[3:])


@pytest.mark.asyncio
async def test_publish_archival_metric_version_never_writes_current_flags() -> None:
    """Archival updates stamp publication metadata without a pointer flip."""
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[0, 0])
    db.execute = AsyncMock(return_value=SimpleNamespace(rowcount=2))
    db.flush = AsyncMock()

    result = await metric_publish.publish_archival_metric_version(
        db,
        version=42,
        competition_ids={7},
        as_of=datetime(2026, 8, 1, 12),
        effective_day=date(2019, 7, 9),
    )

    assert result.contexts == 2
    assert result.seasons == 2
    assert db.flush.await_count == 1
    updates = [
        call.args[0]
        for call in db.execute.await_args_list
        if hasattr(call.args[0], "_values")
    ]
    assert len(updates) == 2
    update_keys = [{str(key) for key in statement._values} for statement in updates]
    assert all(
        not any(key.endswith("is_current") for key in keys) for keys in update_keys
    )
    assert all(
        any(key.endswith("published_at") for key in keys) for keys in update_keys
    )
