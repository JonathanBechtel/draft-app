"""Integration tests for the Summer League ingest cron's schedule-refresh step.

`app/cli/summer_league_ingest_runner.py` (the hourly Summer League ingestion
cron) additively refreshes the active event's *forward* schedule
(``scheduleleaguev2``, via
``app.services.summer_league.scoreboard_ingest.run_scoreboard_ingest``) so
``summer_league_games.tip_datetime`` stays fresh independent of the Summer
League Desk tick's own lifecycle-gated step 0. These tests prove the two
halves of that behavior against a real Postgres session:

* In/near its window, the step runs and upserts real games with
  ``tip_datetime`` -- using the same REAL captured 2026 schedule fixture
  (``scheduleleaguev2_15_2026_live_pretip.json``) as
  ``tests/integration/test_summer_league_scoreboard_ingest.py`` and
  ``tests/integration/test_sl_desk_tick.py``, and a fake/recording NBA Stats
  client (no live network call).
* Off-window, the step makes NO network call at all -- mirrors
  `test_sl_desk_tick.py`'s
  `test_desk_tick_far_off_competition_stays_dormant_without_bootstrap`
  ("no fake client injected, so a regression would attempt a real call and
  fail" pattern). `_refresh_schedule`'s signature always requires an
  already-opened client (this runner's whole point is reusing the one
  ``main()`` opens for its venue loop, never opening a second one), so
  instead of omitting the client entirely a tripwire stand-in is injected:
  any call into it fails the test outright.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import summer_league_ingest_runner as runner
from app.schemas.summer_league import SummerLeagueCompetition, SummerLeagueGame
from app.services.summer_league.nba_stats_client import NBAStatsClient

pytestmark = pytest.mark.asyncio

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "summer_league"
_REAL_LIVE_FIXTURE = _FIXTURE_ROOT / "scheduleleaguev2_15_2026_live_pretip.json"

_N = {"i": 0}


def _next_idx() -> int:
    _N["i"] += 1
    return _N["i"]


def _load_fixture(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())  # type: ignore[no-any-return]


class FakeResponse:
    """Minimal response object mirroring the curl_cffi shape the client reads."""

    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> object:
        """Return the configured JSON payload."""
        return self.payload


class FakeSession:
    """Fake curl_cffi-compatible session that never touches the network.

    Routes responses by the request's ``LeagueID`` param, mirroring
    ``tests/integration/test_summer_league_scoreboard_ingest.py``'s fake.
    """

    def __init__(self, responses_by_league: dict[str, FakeResponse]) -> None:
        self.responses_by_league = responses_by_league
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params: dict[str, str]) -> FakeResponse:
        """Record the call and return the response registered for its LeagueID."""
        self.calls.append((url, params))
        league_id = params.get("LeagueID", "")
        if league_id not in self.responses_by_league:
            return FakeResponse({}, status_code=404)
        return self.responses_by_league[league_id]

    def close(self) -> None:
        """No-op close (matches the real session's interface)."""


class _NetworkTripwireClient:
    """Client stand-in that fails the test if any of its methods are invoked.

    Stronger, more deterministic proof than "would try to open a real client
    and hang" (the pattern `test_sl_desk_tick.py` relies on for functions
    that own an *optional* client): `_refresh_schedule` always receives an
    already-opened client, so this fake is passed in its place and asserts
    directly that nothing ever calls into it.
    """

    def fetch_json(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError(
            "no schedule/scoreboard network call should be attempted off-window"
        )

    def close(self) -> None:
        raise AssertionError("off-window run should never even close a client here")


async def _seed_competition(
    db: AsyncSession,
    *,
    year: int,
    league_id: str = "15",
    venue_slug: str = "las_vegas",
    starts_on: date | None,
    ends_on: date | None,
) -> SummerLeagueCompetition:
    idx = _next_idx()
    comp = SummerLeagueCompetition(
        year=year,
        league_id=league_id,
        venue_slug=f"{venue_slug}-{idx}",
        display_name=f"{year} {venue_slug}",
        starts_on=starts_on,
        ends_on=ends_on,
    )
    db.add(comp)
    await db.flush()
    assert comp.id is not None
    return comp


async def test_refresh_schedule_in_window_upserts_games_with_tip_datetime(
    db_session: AsyncSession,
) -> None:
    """An in-window run calls ``run_scoreboard_ingest`` and persists real tip times.

    Seeds a 2026 Las Vegas competition whose ``starts_on``/``ends_on`` (as
    `normalization.refresh_competition_date_window` would have already set
    them from some earlier real game) span the real fixture's full schedule,
    with zero ``summer_league_games`` rows yet -- the exact "cron runs before
    the Desk has ever woken up" scenario this ticket decouples. `now` falls
    squarely inside that window (Active phase), so
    `runner._schedule_pull_in_window` should allow the call.
    """
    year = 2026
    await _seed_competition(
        db_session,
        year=year,
        league_id="15",
        starts_on=date(year, 7, 9),
        ends_on=date(year, 7, 19),
    )
    await db_session.commit()

    assert (await db_session.execute(select(SummerLeagueGame))).scalars().all() == []
    # `_refresh_schedule` opens its own `db.begin()` (mirrors `_run_venue`'s
    # per-step transaction, isolated from the venue loop) -- commit here so
    # the read above doesn't leave an ambient auto-begun transaction that
    # would conflict with it.
    await db_session.commit()

    payload = _load_fixture(_REAL_LIVE_FIXTURE)
    session = FakeSession({"15": FakeResponse(payload)})
    client = NBAStatsClient(session=session)

    now = datetime(year, 7, 12, 18, 0, tzinfo=timezone.utc)  # mid-event, 2:00pm ET
    report = await runner._refresh_schedule(db_session, now=now, client=client)
    await db_session.commit()

    assert report is not None
    assert report.games_created == 76
    assert report.errors == []

    # No live network call: the fake session recorded exactly one GET.
    assert len(session.calls) == 1
    _, called_params = session.calls[0]
    assert called_params["LeagueID"] == "15"

    games = (await db_session.execute(select(SummerLeagueGame))).scalars().all()
    assert len(games) == 76
    assert all(g.tip_datetime is not None for g in games)

    first_game = (
        await db_session.execute(
            select(SummerLeagueGame).where(
                SummerLeagueGame.nba_stats_game_id == "1522600001"  # type: ignore[arg-type]
            )
        )
    ).scalar_one()
    # tip_datetime is stored naive (UTC by convention, like the rest of this
    # schema); the fixture's first game's real gameDateTimeUTC.
    assert first_game.tip_datetime == datetime(year, 7, 9, 19, 30)
    assert first_game.game_date == date(year, 7, 9)


async def test_schedule_pull_in_window_true_for_seeded_active_competition(
    db_session: AsyncSession,
) -> None:
    """Direct DB-driven check: the window guard reads real ``starts_on``/``ends_on``.

    No monkeypatching of `resolve_target_competitions` here (unlike the unit
    tests) -- proves the guard's own query resolves a real competition row
    and feeds it through `lifecycle_phase` correctly.
    """
    year = 2026
    await _seed_competition(
        db_session,
        year=year,
        starts_on=date(year, 7, 9),
        ends_on=date(year, 7, 19),
    )
    await db_session.commit()

    now = datetime(year, 7, 12, 18, 0, tzinfo=timezone.utc)
    assert await runner._schedule_pull_in_window(db_session, now=now) is True


async def test_refresh_schedule_off_window_makes_no_network_call(
    db_session: AsyncSession,
) -> None:
    """A competition far outside its window -- no schedule/scoreboard network call.

    No games exist before or after the call, and the tripwire client proves
    `run_scoreboard_ingest` (and therefore any NBA Stats request) was never
    reached.
    """
    year = 2026
    await _seed_competition(
        db_session,
        year=year,
        starts_on=date(year, 7, 9),
        ends_on=date(year, 7, 19),
    )
    await db_session.commit()

    # Three months before `announce_horizon_days` (14) even opens the window
    # -- mirrors `test_sl_desk_tick.py`'s far-off-dormant test.
    now = datetime(year, 3, 1, 12, 0, tzinfo=timezone.utc)

    result = await runner._refresh_schedule(
        db_session,
        now=now,
        client=_NetworkTripwireClient(),  # type: ignore[arg-type]
    )
    await db_session.commit()

    assert result is None
    assert (await db_session.execute(select(SummerLeagueGame))).scalars().all() == []


async def test_refresh_schedule_cold_start_no_configured_window_makes_no_network_call(
    db_session: AsyncSession,
) -> None:
    """A competition registered with no ``starts_on``/``ends_on`` yet -- no network call.

    The true first-ever cold start (mirrors
    `runner._schedule_pull_in_window`'s own docstring trade-off): with no
    real games and no configured window, there is no signal to reason about,
    so this stays network-free rather than guessing.
    """
    year = 2026
    await _seed_competition(
        db_session,
        year=year,
        starts_on=None,
        ends_on=None,
    )
    await db_session.commit()

    now = datetime(year, 7, 12, 18, 0, tzinfo=timezone.utc)

    result = await runner._refresh_schedule(
        db_session,
        now=now,
        client=_NetworkTripwireClient(),  # type: ignore[arg-type]
    )
    await db_session.commit()

    assert result is None
    assert (await db_session.execute(select(SummerLeagueGame))).scalars().all() == []
