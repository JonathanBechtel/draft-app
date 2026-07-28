"""Regression tests for the deferred player-embedding listener.

The embedding write must be scheduled only *after* the surrounding transaction
commits — scheduling it during the flush (the previous behaviour) races the
caller's commit and can persist an embedding for a row that later rolls back.
These tests pin the deferral: collect-on-flush, fire-on-commit, drop-on-rollback.
"""

import contextvars
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from app.schemas import players_master as pm
from app.schemas.players_master import (
    _PENDING_EMBEDDINGS_KEY,
    PlayerMaster,
    collect_inserted_players_for_embedding,
    discard_uncommitted_player_embeddings,
    embed_committed_players,
)
from app.utils import network_guard


def _fake_session(new: list[Any] | None = None) -> Any:
    """A stand-in session exposing just the `.new` and `.info` the handlers use."""
    return SimpleNamespace(new=new or [], info={})


def test_after_flush_collects_player_snapshot() -> None:
    """Newly-inserted players are snapshotted into session.info on flush."""
    player = PlayerMaster(
        id=42, display_name="Aday Mara", school="Michigan", birth_country="Spain"
    )
    session = _fake_session(new=[player])

    collect_inserted_players_for_embedding(session, None)

    assert session.info[_PENDING_EMBEDDINGS_KEY] == [
        {
            "player_id": 42,
            "display_name": "Aday Mara",
            "school": "Michigan",
            "birth_country": "Spain",
        }
    ]


def test_after_flush_ignores_non_players_and_pkless_rows() -> None:
    """Non-PlayerMaster objects and players without a PK are skipped."""
    pkless = PlayerMaster(id=None, display_name="No PK")
    other = SimpleNamespace(id=1)  # not a PlayerMaster
    session = _fake_session(new=[pkless, other])

    collect_inserted_players_for_embedding(session, None)

    assert session.info[_PENDING_EMBEDDINGS_KEY] == []


def test_flush_does_not_schedule_embedding_before_commit() -> None:
    """The race regression: flush must NOT fire the embedding task."""
    player = PlayerMaster(id=7, display_name="Cooper Flagg", school="Duke")
    session = _fake_session(new=[player])

    with patch.object(pm, "_schedule_player_embedding") as sched:
        collect_inserted_players_for_embedding(session, None)
        sched.assert_not_called()


def test_after_commit_schedules_each_pending_player_then_clears() -> None:
    """Commit fires one embedding task per collected snapshot, then clears state."""
    player = PlayerMaster(id=7, display_name="Cooper Flagg", school="Duke")
    session = _fake_session(new=[player])
    collect_inserted_players_for_embedding(session, None)

    with patch.object(pm, "_schedule_player_embedding") as sched:
        embed_committed_players(session)

    sched.assert_called_once()
    assert sched.call_args.args[0]["player_id"] == 7
    assert _PENDING_EMBEDDINGS_KEY not in session.info


def test_after_rollback_discards_without_scheduling() -> None:
    """Rolled-back inserts are dropped and never embedded, even on a later commit."""
    player = PlayerMaster(id=7, display_name="Rolled Back")
    session = _fake_session(new=[player])
    collect_inserted_players_for_embedding(session, None)

    with patch.object(pm, "_schedule_player_embedding") as sched:
        discard_uncommitted_player_embeddings(session)
        embed_committed_players(session)  # nothing pending -> no-op
        sched.assert_not_called()

    assert _PENDING_EMBEDDINGS_KEY not in session.info


def test_scheduled_embedding_uses_clean_context() -> None:
    """Post-commit tasks must not inherit the committing transaction marker."""
    captured: list[contextvars.Context] = []

    class _Loop:
        def is_running(self) -> bool:
            return True

        def create_task(self, coroutine, *, context):
            captured.append(context)
            coroutine.close()

    token = network_guard._active_transaction_ids.set(frozenset({123}))
    try:
        with patch.object(pm.asyncio, "get_event_loop", return_value=_Loop()):
            pm._schedule_player_embedding(
                {
                    "player_id": 7,
                    "display_name": "Cooper Flagg",
                    "school": "Duke",
                    "birth_country": "USA",
                }
            )
    finally:
        network_guard._active_transaction_ids.reset(token)

    assert len(captured) == 1
    assert captured[0].run(network_guard.transaction_depth) == 0
