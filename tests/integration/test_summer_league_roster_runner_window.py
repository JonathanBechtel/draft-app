"""Integration test for the roster cron's year/force window semantics (#717).

`app/cli/summer_league_roster_runner.py`'s `SL_ROSTER_YEAR` env var used to
bypass the shared Event Desk lifecycle window unconditionally the moment it
was set -- a code change was required every season to keep the cron from
either bypassing safety gating forever (once the configured year's event
ended) or going silently dormant all year (if left at the old hard-coded
default). This proves the fix end-to-end through `runner.main()` against a
real Postgres session: `SL_ROSTER_YEAR` now only *scopes* which year's
competitions the window check considers, so a finished season stays dormant
and the run never opens a roster fetch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cli import summer_league_roster_runner as runner
from app.schemas.summer_league import SummerLeagueCompetition

pytestmark = pytest.mark.asyncio


class _SchemaScopedSessionContext:
    """Async context manager mirroring ``SessionLocal()`` with the test schema set.

    `runner.main()` opens its own session via `SessionLocal()` internally,
    rather than accepting one as a parameter. Wrapping `session_factory` here
    (instead of the real app-configured `SessionLocal`) keeps the run bound to
    the isolated integration-test schema/transaction, the same way `db_session`
    itself is set up in `conftest.py`.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession], schema: str) -> None:
        self._factory = factory
        self._schema = schema
        self._cm: object = None

    async def __aenter__(self) -> AsyncSession:
        self._cm = self._factory()
        session = await self._cm.__aenter__()  # type: ignore[attr-defined]
        await session.execute(text(f'SET search_path TO "{self._schema}"'))
        await session.commit()
        return session

    async def __aexit__(self, *exc_info: object) -> None:
        await self._cm.__aexit__(*exc_info)  # type: ignore[attr-defined]


class _NetworkTripwireRosterFetcher:
    """RosterFetcher stand-in that fails the test if it ever fetches a roster.

    Construction is allowed (the real `main()` builds the fetcher before the
    window check runs), but `fetch_run` -- the only network-triggering call --
    must never be reached while the run is dormant.
    """

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def fetch_run(self, **_kwargs: object) -> object:
        raise AssertionError("off-window roster poll should never fetch a roster")


async def test_finished_year_roster_year_stays_dormant_with_zero_http(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    test_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SL_ROSTER_YEAR scoped to an already-ended season makes zero HTTP calls.

    Seeds a real competition for the *current* calendar year (so
    `resolve_target_competitions`'s today's-year fallback has something to
    resolve even with no active `events` row) whose date window already
    closed. Under the pre-fix behavior, setting `SL_ROSTER_YEAR` alone would
    have returned True unconditionally regardless of this competition's
    state; the fix must instead scope to it, evaluate its lifecycle phase as
    closed, and keep `main()` dormant -- exit 0, zero venues touched, the
    tripwire fetcher's `fetch_run` never called.
    """
    today = datetime.now(timezone.utc).date()
    year = today.year
    starts_on = today - timedelta(days=60)
    ends_on = today - timedelta(days=50)

    competition = SummerLeagueCompetition(
        year=year,
        league_id="15",
        venue_slug="las-vegas-717-window",
        display_name=f"{year} Las Vegas (ended)",
        starts_on=starts_on,
        ends_on=ends_on,
    )
    db_session.add(competition)
    await db_session.flush()
    await db_session.commit()

    monkeypatch.setenv("SL_ROSTER_YEAR", str(year))
    monkeypatch.delenv("SL_ROSTER_FORCE", raising=False)
    monkeypatch.setenv("SL_ROSTER_LEAGUE_IDS", "15")

    async def _fake_dispose() -> None:
        """Avoid disposing the real app-configured engine (not under test)."""

    monkeypatch.setattr(
        runner,
        "SessionLocal",
        lambda: _SchemaScopedSessionContext(session_factory, test_schema),
    )
    monkeypatch.setattr(runner, "dispose_engine", _fake_dispose)
    monkeypatch.setattr(runner, "RosterFetcher", _NetworkTripwireRosterFetcher)

    result = await runner.main()

    assert result == 0
