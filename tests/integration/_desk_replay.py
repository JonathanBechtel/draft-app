"""Live-window replay harness for the Desk latency-class partition (#699).

Why this exists
---------------
#699 is reliability work whose whole value is *under live-game load*, and
Summer League 2026 ended 2026-07-19. There are no live games to poll, so a
naive local run of the new fast poller hits the off-window path and logs
``off-window (dormant) -- no-op``. Building the partition and merging it on the
assumption it will be fine next July is precisely the shape the failure record
indicts: code that lies dormant until the event it exists to protect, then runs
in production for the first time.

The ticket offered two honest options -- defer, or **build it behind a staging
replay**. This module is that replay. It steps a recorded event's raw provider
JSON through the partitioned tick so the acceptance property ("the fast class
stays responsive while the slow class runs") is verifiable *today*, off-season,
in CI, on every future change.

Fixture provenance -- read before adding a frame
------------------------------------------------
This repo has a standing guardrail: the provider fixture driving a Desk test
must be a **real captured NBA payload**, not a hand-authored dict, and any
unavoidable fabrication must be labeled as such (see
``test_sl_desk_tick.py::test_desk_tick_two_sequential_ticks_over_real_schedule_never_finalize_a_scheduled_game``).
This harness honors that:

* ``scheduleleaguev2_15_2026_live_pretip.json`` is a **verbatim capture** from
  stats.nba.com (LeagueID 15, Season 2026) taken 2026-07-10 ~19:34 UTC: seven
  real Final games plus real not-yet-tipped Scheduled games through 2026-07-19.
* :func:`real_live_window_frames` replays that capture **unmodified**, advancing
  only the tick's ``now``. Every frame it yields is real provider content. This
  is what the acceptance test runs on, so the acceptance signal depends on zero
  fabricated data.
* :func:`derived_in_progress_frame` is the one **derived** frame: it takes a
  real Scheduled game from the same capture and advances it to
  ``gameStatus == 2`` with partial scores. No genuinely in-progress capture
  exists anywhere in this repo (checked the SL fixtures dir and #529/#531's
  captured assets), and this sandbox has no live network to fetch one. It is
  labeled ``derived=True`` and is used only to show scores advancing under
  contention -- never as the acceptance signal.

What "responsive" is measured against
--------------------------------------
Spec §2's success metric is *the percentage of scheduled ticks that complete
with advanced source data, measured specifically within live-game windows --
not averaged across the day*, because off-peak ticks succeed easily and mask
exactly the live-window misses that are the entire user-visible problem.
:class:`ReplayOutcome` reports that number for the frames replayed, and nothing
else: there is deliberately no daily-average accessor to reach for.
"""

from __future__ import annotations

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, AsyncIterator, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.summer_league.write_lock import (
    acquire_summer_league_writer_lock,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "summer_league"
REAL_LIVE_CAPTURE = FIXTURE_ROOT / "scheduleleaguev2_15_2026_live_pretip.json"

#: The capture's reference instant. Six real Scheduled games tip inside
#: ``[now-6h, now+6h]``; all seven real Final games tip before the window
#: opens. Lifted from #531's own proven expectations, not re-derived.
CAPTURE_REFERENCE_NOW = datetime(2026, 7, 10, 19, 30)


def load_real_capture() -> dict[str, Any]:
    """Load the verbatim 2026 live-pretip schedule capture."""
    return json.loads(REAL_LIVE_CAPTURE.read_text())  # type: ignore[no-any-return]


@dataclass(frozen=True)
class ReplayFrame:
    """One replayed provider snapshot plus the instant it is replayed at.

    Attributes:
        label: Human-readable frame name, used in assertion messages so a
            failure names *which* frame regressed.
        now: The tick's reference instant for this frame.
        scoreboard_payload: The ``scheduleleaguev2`` response to serve.
        derived: Whether ``scoreboard_payload`` was modified from the verbatim
            capture. ``False`` means real provider content, byte-for-byte.
    """

    label: str
    now: datetime
    scoreboard_payload: dict[str, Any]
    derived: bool = False


def real_live_window_frames(
    *,
    count: int = 6,
    step: timedelta = timedelta(minutes=10),
    start: datetime = CAPTURE_REFERENCE_NOW,
) -> list[ReplayFrame]:
    """Replay the verbatim capture across a sliding live window.

    Each frame serves the **unmodified** captured payload at a later ``now``,
    which is exactly how a real poller experiences an event: the provider
    document is re-fetched every few minutes and the set of games inside the
    active window shifts forward as the evening progresses. Nothing here is
    fabricated -- the only thing that changes between frames is the clock.

    Args:
        count: How many frames to replay.
        step: Wall-clock spacing between frames. Ten minutes matches the fast
            class's minutes-cadence intent.
        start: The first frame's instant.

    Returns:
        ``count`` frames, all ``derived=False``.
    """
    payload = load_real_capture()
    return [
        ReplayFrame(
            label=f"real+{int((step * index).total_seconds() // 60)}min",
            now=start + step * index,
            scoreboard_payload=payload,
            derived=False,
        )
        for index in range(count)
    ]


def derived_in_progress_frame(
    *,
    nba_stats_game_id: str,
    now: datetime,
    home_score: int,
    away_score: int,
    status_text: str = "Q3 4:21",
) -> ReplayFrame:
    """A real Scheduled game advanced to in-progress. **Derived, not captured.**

    No genuinely in-progress (``gameStatus == 2``) capture exists in this repo
    and this sandbox cannot fetch one, so proving "scores advance while a game
    is live and the backbone holds the lock" requires exactly this one
    fabrication. It is deliberately minimal: the surrounding document, team
    IDs, tip times, and every other game stay verbatim; only the target game's
    ``gameStatus``/``gameStatusText`` and the two team scores are touched.

    Args:
        nba_stats_game_id: A game ID present in the capture as Scheduled.
        now: The instant to replay this frame at.
        home_score: Partial home score to report.
        away_score: Partial away score to report.
        status_text: Provider status text for the in-progress state.

    Returns:
        A frame flagged ``derived=True``.

    Raises:
        AssertionError: The game ID is absent from the capture, or was not
            Scheduled there -- either would make this frame silently
            meaningless.
    """
    payload = copy.deepcopy(load_real_capture())
    found = False
    for game_date in payload["leagueSchedule"]["gameDates"]:
        for game in game_date["games"]:
            if game["gameId"] != nba_stats_game_id:
                continue
            assert game["gameStatus"] == 1, (
                f"{nba_stats_game_id} is not Scheduled in the capture "
                f"(gameStatus={game['gameStatus']}); deriving an in-progress "
                "frame from it would assert nothing."
            )
            game["gameStatus"] = 2
            game["gameStatusText"] = status_text
            game["homeTeam"]["score"] = home_score
            game["awayTeam"]["score"] = away_score
            found = True
    assert found, f"{nba_stats_game_id} is not present in the real capture"
    return ReplayFrame(
        label=f"derived-in-progress({nba_stats_game_id})",
        now=now,
        scoreboard_payload=payload,
        derived=True,
    )


class ReplayResponse:
    """Minimal response object mirroring the curl_cffi shape the client reads."""

    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> object:
        """Return the configured JSON payload."""
        return self.payload


class ReplaySession:
    """Fake curl_cffi session that serves whichever frame is currently active.

    ``scheduleleaguev2`` returns the active frame's payload; every other
    endpoint (``leaguegamelog`` and the five per-game box/pbp/shot endpoints)
    gets a trivial ``{"resultSets": []}``. That mirrors the existing Desk
    tests' convention and is honest here: this harness asserts on
    scoreboard-derived state and on *timing under contention*, never on
    per-game raw content, and there is no real captured 2026 response for
    those endpoints to serve instead.
    """

    def __init__(self) -> None:
        self._frame: Optional[ReplayFrame] = None
        self.calls: list[tuple[str, dict[str, str]]] = []

    def use(self, frame: ReplayFrame) -> None:
        """Make ``frame``'s payload the one ``scheduleleaguev2`` returns."""
        self._frame = frame

    def get(self, url: str, params: dict[str, str]) -> ReplayResponse:
        """Record the call and serve the active frame (or an empty result set)."""
        self.calls.append((url, dict(params)))
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint == "scheduleleaguev2" and self._frame is not None:
            return ReplayResponse(self._frame.scoreboard_payload)
        return ReplayResponse({"resultSets": []})

    def close(self) -> None:
        """No-op close (matches the real session's interface)."""


@dataclass
class FrameOutcome:
    """What one replayed frame did, and how long it took."""

    label: str
    derived: bool
    completed: bool
    source_advanced: bool
    duration_seconds: float
    error: Optional[BaseException] = None


@dataclass
class ReplayOutcome:
    """Aggregate result of a replay, reported the way spec §2 requires.

    There is intentionally no "average across the day" accessor: the metric
    that matters is completion *inside the live window*, and a daily average is
    precisely what hid this failure in production.
    """

    frames: list[FrameOutcome] = field(default_factory=list)

    @property
    def completion_rate(self) -> float:
        """Fraction of replayed frames that completed without raising."""
        if not self.frames:
            return 0.0
        return sum(1 for f in self.frames if f.completed) / len(self.frames)

    @property
    def advanced_rate(self) -> float:
        """Fraction that completed **with advanced source data**.

        This -- not bare completion -- is the ticket's acceptance signal. A
        tick that returns successfully having moved nothing forward is
        indistinguishable to a visitor from one that never ran.
        """
        if not self.frames:
            return 0.0
        return sum(1 for f in self.frames if f.source_advanced) / len(self.frames)

    @property
    def slowest_seconds(self) -> float:
        """The worst single-frame wall-clock time."""
        return max((f.duration_seconds for f in self.frames), default=0.0)

    @property
    def failures(self) -> list[FrameOutcome]:
        """Frames that raised."""
        return [f for f in self.frames if not f.completed]

    def describe(self) -> str:
        """A one-line summary suitable for an assertion message."""
        return (
            f"completed={self.completion_rate:.0%} advanced={self.advanced_rate:.0%} "
            f"slowest={self.slowest_seconds:.2f}s "
            f"failures={[f.label for f in self.failures]}"
        )


@asynccontextmanager
async def backbone_holding_writer_lock(
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
    *,
    hold_seconds: float,
    hold_game_rows: bool = False,
) -> AsyncIterator[asyncio.Task[None]]:
    """Run a stand-in backbone that holds the shared writer lock throughout.

    This reproduces the *exact mechanism* of the production failure: the
    ~88-minute venue ingest did not harm the Desk by consuming CPU or database
    throughput, it harmed it by holding this advisory lock while the Desk's
    bounded wait expired and the interval was skipped. Holding the real lock
    from a genuinely separate session, concurrently, is therefore a faithful
    stand-in for the slow class -- scaled from 88 minutes to a few seconds so
    it runs in CI.

    The task is cancelled and awaited on exit, so a failing test can never leak
    a lock holder into the next one.

    Args:
        session_factory: Factory for the independent holder session.
        test_schema: Schema to pin the holder to. The advisory-lock key is
            ``hashtext(current_schema())``, so a holder on the wrong schema
            would contend on an unrelated lock and silently prove nothing.
        hold_seconds: How long to hold the lock.
        hold_game_rows: Also lock every existing ``summer_league_games`` row
            with ``SELECT ... FOR UPDATE``. This models the normalization
            phase's row-level contention separately from the advisory lock.

    Yields:
        The running holder task, so a caller can assert on its state.
    """
    holding = asyncio.Event()

    async def _hold() -> None:
        async with session_factory() as holder:
            await holder.execute(text(f'SET search_path TO "{test_schema}"'))
            await holder.commit()
            async with holder.begin():
                await acquire_summer_league_writer_lock(holder)
                if hold_game_rows:
                    await holder.execute(
                        text("SELECT id FROM summer_league_games FOR UPDATE")
                    )
                holding.set()
                await asyncio.sleep(hold_seconds)

    task = asyncio.create_task(_hold())
    try:
        # Do not start replaying until the lock is genuinely held, or the
        # replay could finish before contention ever begins and the test would
        # pass vacuously.
        await asyncio.wait_for(holding.wait(), timeout=30.0)
        yield task
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def replay_frames(
    frames: Sequence[ReplayFrame],
    session: ReplaySession,
    *,
    run_frame: Any,
) -> ReplayOutcome:
    """Drive ``frames`` through ``run_frame``, timing and recording each.

    A frame that raises is recorded as a failure and the replay continues, so
    one bad frame reports as "5/6 completed" rather than aborting the run and
    hiding how the remaining frames would have behaved.

    Args:
        frames: The frames to replay, in order.
        session: The fake provider session; its active frame is switched
            before each call.
        run_frame: ``async (frame) -> object`` running one tick. Its result is
            inspected for a truthy ``source_advanced`` attribute.

    Returns:
        A :class:`ReplayOutcome`.
    """
    outcome = ReplayOutcome()
    for frame in frames:
        session.use(frame)
        started_at = perf_counter()
        try:
            result = await run_frame(frame)
        except Exception as exc:  # noqa: BLE001 -- recorded, then reported
            # Deliberately not BaseException: a lock timeout is an Exception,
            # while KeyboardInterrupt/CancelledError must abort the replay
            # rather than be recorded as a "failed frame" and swallowed.
            outcome.frames.append(
                FrameOutcome(
                    label=frame.label,
                    derived=frame.derived,
                    completed=False,
                    source_advanced=False,
                    duration_seconds=perf_counter() - started_at,
                    error=exc,
                )
            )
            continue
        outcome.frames.append(
            FrameOutcome(
                label=frame.label,
                derived=frame.derived,
                completed=True,
                source_advanced=bool(getattr(result, "source_advanced", False)),
                duration_seconds=perf_counter() - started_at,
            )
        )
    return outcome
