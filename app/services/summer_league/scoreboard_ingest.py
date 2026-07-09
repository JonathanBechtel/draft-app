"""Job B step 0 — Summer League scoreboard/schedule ingest.

Fetches today's and tomorrow's Summer League games from the stats.nba.com
schedule feed and upserts ``tip_datetime`` + live status onto
``summer_league_games`` **before** normalization or any Desk projection runs
(`docs/plans/summer-league-scouts-desk-behavior-spec.md` §2 "Resolution & data
prerequisites", §10 Job B step 0). The Morning Card and the Ledger->Morning
state-machine flip cannot exist without tip times, so this step runs first in
every tick.

**Reuses, doesn't re-derive:**

* :class:`~app.services.summer_league.nba_stats_client.NBAStatsClient` — the
  existing ``curl_cffi`` Chrome-impersonation client every other Summer League
  scraper uses to get past stats.nba.com's bot-management.
* :func:`~app.services.summer_league.endpoints.build_schedule_params` and the
  ``scheduleleaguev2`` endpoint — already wired for bracket-round enrichment
  (``app/services/summer_league/bracket.py``). That feed *is* the scoreboard
  for Summer League: each game carries a ``gameStatus`` code plus
  ``gameDateTimeUTC`` for the whole season, kept live-updated for the current
  date, so no separate NBA Stats endpoint is needed.

**Active competition resolution:** prefers the registered Event Desk row's
``events.calendar_ref["competition_ids"]`` (the framework's intended
resolution path — ticket #506 registers Summer League there). Falls back to
every ``summer_league_competitions`` row for the current year when no active
``events`` row exists yet, which is true at the time this ticket ships (#506
is a sibling ticket, not a dependency of this one) and keeps this step usable
standalone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import Event
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
)
from app.services.summer_league.endpoints import build_schedule_params
from app.services.summer_league.nba_stats_client import NBAStatsAPIError, NBAStatsClient

# Stable key for the (eventually #506-registered) Summer League events row.
EVENT_KEY_SUMMER_LEAGUE = "summer_league"

# NBA Stats schedule-feed ``gameStatus`` codes (scheduleleaguev2 and the
# scoreboard family share this convention): 1 = scheduled, 2 = live,
# 3 = final. Anything else (or a missing code) maps to UNKNOWN.
_STATUS_CODE_MAP: dict[int, SummerLeagueGameStatus] = {
    1: SummerLeagueGameStatus.SCHEDULED,
    2: SummerLeagueGameStatus.IN_PROGRESS,
    3: SummerLeagueGameStatus.FINAL,
}


@dataclass(frozen=True)
class ScoreboardGame:
    """One parsed scoreboard/schedule game row."""

    nba_stats_game_id: str
    game_date: date | None
    tip_datetime: datetime | None
    status: SummerLeagueGameStatus


@dataclass
class ScoreboardIngestReport:
    """Summary of one scoreboard-ingest run across every resolved competition."""

    competitions_checked: int = 0
    games_seen: int = 0
    games_created: int = 0
    games_updated: int = 0
    errors: list[str] = field(default_factory=list)


def map_game_status(
    game_status: int | str | None, game_status_text: str | None = None
) -> SummerLeagueGameStatus:
    """Map an NBA Stats schedule/scoreboard status code to our normalized enum.

    Args:
        game_status: Raw ``gameStatus`` code (``1``/``2``/``3``, sometimes a
            numeric string).
        game_status_text: Raw ``gameStatusText`` (e.g. ``"Final"``, ``"7:00 pm
            ET"``, ``"Qtr 3 - 4:12"``), used as a fallback when the code is
            missing or unrecognized.

    Returns:
        ``SCHEDULED`` / ``IN_PROGRESS`` / ``FINAL`` / ``UNKNOWN``.
    """
    code: int | None = None
    if isinstance(game_status, bool):
        code = None
    elif isinstance(game_status, int):
        code = game_status
    elif isinstance(game_status, str) and game_status.strip().lstrip("-").isdigit():
        code = int(game_status.strip())

    if code is not None and code in _STATUS_CODE_MAP:
        return _STATUS_CODE_MAP[code]

    text = (game_status_text or "").strip().lower()
    if "final" in text:
        return SummerLeagueGameStatus.FINAL
    if "qtr" in text or "half" in text or "progress" in text:
        return SummerLeagueGameStatus.IN_PROGRESS
    return SummerLeagueGameStatus.UNKNOWN


def parse_tip_datetime_utc(game: Mapping[str, Any]) -> datetime | None:
    """Parse a scoreboard/schedule game's tip-off time as an aware UTC datetime.

    Prefers ``gameDateTimeUTC`` (carries the correct date *and* time). Falls
    back to combining ``gameDateUTC``'s date with ``gameTimeUTC``'s
    time-of-day when ``gameDateTimeUTC`` is absent or unparseable --
    ``gameTimeUTC`` alone carries a placeholder ``1900-01-01`` date on the NBA
    Stats schedule feed.

    Args:
        game: One raw game dict from the ``scheduleleaguev2`` payload.

    Returns:
        An aware UTC ``datetime``, or ``None`` when no usable timestamp exists.
    """
    combined = _parse_iso(game.get("gameDateTimeUTC"))
    if combined is not None:
        return combined

    date_part = _parse_iso(game.get("gameDateUTC"))
    time_part = _parse_iso(game.get("gameTimeUTC"))
    if date_part is not None and time_part is not None:
        return datetime.combine(date_part.date(), time_part.time(), tzinfo=timezone.utc)
    return None


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp string, returning an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_naive_utc(value: datetime | None) -> datetime | None:
    """Strip tzinfo from an aware UTC datetime for the naive TIMESTAMP column.

    ``summer_league_games.tip_datetime`` is UTC by convention (like this
    package's other timestamp columns) but stored as a naive
    ``TIMESTAMP WITHOUT TIME ZONE``, matching the rest of this schema.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _parse_game_date(
    game: Mapping[str, Any], tip_datetime: datetime | None
) -> date | None:
    """Resolve the schedule "game day" for one raw game dict.

    Args:
        game: One raw game dict from the ``scheduleleaguev2`` payload.
        tip_datetime: Already-parsed tip time, used as a last-resort fallback.

    Returns:
        The game's calendar date, or ``None`` when nothing usable is present.
    """
    for key in ("gameDate", "gameDateEst", "gameDateUTC"):
        raw = game.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        parsed = _parse_iso(text)
        if parsed is not None:
            return parsed.date()
    if tip_datetime is not None:
        return tip_datetime.date()
    return None


def parse_scoreboard_games(
    payload: Mapping[str, Any], *, target_dates: Iterable[date]
) -> list[ScoreboardGame]:
    """Parse a ``scheduleleaguev2`` payload into games tipping on the target dates.

    Args:
        payload: Parsed NBA Stats ``scheduleleaguev2`` JSON response.
        target_dates: Calendar dates to keep; Job B step 0 passes
            ``{today, tomorrow}``.

    Returns:
        One :class:`ScoreboardGame` per game whose schedule date falls in
        ``target_dates``, in payload order.
    """
    wanted = set(target_dates)
    schedule = payload.get("leagueSchedule") or {}
    games: list[ScoreboardGame] = []
    for game_date_block in schedule.get("gameDates") or []:
        for game in game_date_block.get("games") or []:
            game_id = str(game.get("gameId") or "").strip()
            if not game_id:
                continue
            tip_datetime = parse_tip_datetime_utc(game)
            game_date = _parse_game_date(game, tip_datetime)
            if game_date is None or game_date not in wanted:
                continue
            status = map_game_status(game.get("gameStatus"), game.get("gameStatusText"))
            games.append(
                ScoreboardGame(
                    nba_stats_game_id=game_id,
                    game_date=game_date,
                    tip_datetime=tip_datetime,
                    status=status,
                )
            )
    return games


async def resolve_target_competitions(
    db: AsyncSession, *, today: date, event_key: str = EVENT_KEY_SUMMER_LEAGUE
) -> list[SummerLeagueCompetition]:
    """Resolve which Summer League competitions Job B step 0 should poll today.

    Prefers the registered ``events`` row's ``calendar_ref["competition_ids"]``
    (the Event Desk framework's intended resolution path -- ticket #506's SL
    registration). Falls back to every competition for ``today``'s year when
    no active ``events`` row exists yet, so this step works standalone before
    #506 lands and continues to work unchanged after it does.

    Args:
        db: Async database session.
        today: The current date, used for the fallback year filter.
        event_key: The registered event's stable key. Defaults to
            ``"summer_league"``.

    Returns:
        The resolved :class:`SummerLeagueCompetition` rows (possibly empty).
    """
    event_stmt = select(Event).where(
        Event.key == event_key,  # type: ignore[arg-type]
        Event.is_active.is_(True),  # type: ignore[attr-defined]
    )
    event = (await db.execute(event_stmt)).scalar_one_or_none()

    competition_ids: list[int] | None = None
    if event is not None:
        raw_ids = event.calendar_ref.get("competition_ids")
        if isinstance(raw_ids, list) and raw_ids:
            competition_ids = [int(raw_id) for raw_id in raw_ids]

    if competition_ids is not None:
        comp_stmt = select(SummerLeagueCompetition).where(
            SummerLeagueCompetition.id.in_(competition_ids)  # type: ignore[union-attr]
        )
    else:
        comp_stmt = select(SummerLeagueCompetition).where(
            SummerLeagueCompetition.year == today.year  # type: ignore[arg-type]
        )
    result = await db.execute(comp_stmt)
    return list(result.scalars().all())


async def upsert_scoreboard_games(
    db: AsyncSession, *, competition_id: int, games: Iterable[ScoreboardGame]
) -> tuple[int, int]:
    """Create or update ``summer_league_games`` rows from parsed scoreboard games.

    Only touches ``game_date`` / ``tip_datetime`` / ``status`` -- box-score
    fields (teams, score) stay owned by ``normalize_summer_league`` and are
    left untouched on existing rows.

    Args:
        db: Async database session (caller controls the transaction/commit).
        competition_id: The competition new rows should be created under.
        games: Parsed scoreboard games, e.g. from :func:`parse_scoreboard_games`.

    Returns:
        ``(created, updated)`` row counts.
    """
    created = 0
    updated = 0
    for game in games:
        stmt = select(SummerLeagueGame).where(
            SummerLeagueGame.nba_stats_game_id == game.nba_stats_game_id  # type: ignore[arg-type]
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()
        tip_datetime = _to_naive_utc(game.tip_datetime)
        if existing is None:
            db.add(
                SummerLeagueGame(
                    competition_id=competition_id,
                    nba_stats_game_id=game.nba_stats_game_id,
                    game_date=game.game_date,
                    tip_datetime=tip_datetime,
                    status=game.status,
                )
            )
            created += 1
        else:
            if game.game_date is not None:
                existing.game_date = game.game_date
            if tip_datetime is not None:
                existing.tip_datetime = tip_datetime
            existing.status = game.status
            updated += 1
    await db.flush()
    return created, updated


async def run_scoreboard_ingest(
    db: AsyncSession,
    *,
    today: date | None = None,
    client: NBAStatsClient | None = None,
) -> ScoreboardIngestReport:
    """Job B step 0 -- fetch and upsert today's and tomorrow's SL games.

    Resolves the active competition(s) (:func:`resolve_target_competitions`),
    fetches each one's ``scheduleleaguev2`` feed via the shared ``curl_cffi``
    :class:`NBAStatsClient`, filters to games tipping today or tomorrow, maps
    NBA Stats status codes to :class:`SummerLeagueGameStatus`, and upserts
    ``tip_datetime`` / ``status`` onto ``summer_league_games`` -- before
    normalization or the rest of the desk tick runs (behavior spec §10 Job B
    step 0). The caller owns the session commit, matching every other Summer
    League ingest step in this package.

    Args:
        db: Async database session (caller controls the transaction).
        today: Override for "today" (tests only); defaults to the current UTC
            date.
        client: Optional injected :class:`NBAStatsClient` (tests only); when
            omitted a real client is opened for the duration of the run and
            closed afterward.

    Returns:
        Aggregate counts across every resolved competition, plus any
        per-competition fetch errors (a failed competition does not abort the
        others).
    """
    resolved_today = today or datetime.now(timezone.utc).date()
    tomorrow = resolved_today + timedelta(days=1)
    target_dates = {resolved_today, tomorrow}

    report = ScoreboardIngestReport()
    competitions = await resolve_target_competitions(db, today=resolved_today)
    report.competitions_checked = len(competitions)
    if not competitions:
        return report

    owns_client = client is None
    active_client = client or NBAStatsClient()
    try:
        for competition in competitions:
            try:
                payload = active_client.fetch_json(
                    "scheduleleaguev2",
                    build_schedule_params(
                        league_id=competition.league_id, season=competition.year
                    ),
                )
            except NBAStatsAPIError as exc:
                report.errors.append(
                    f"{competition.year}/{competition.league_id}: {exc}"
                )
                continue

            games = parse_scoreboard_games(payload, target_dates=target_dates)
            report.games_seen += len(games)
            assert competition.id is not None
            created, updated = await upsert_scoreboard_games(
                db, competition_id=competition.id, games=games
            )
            report.games_created += created
            report.games_updated += updated
    finally:
        if owns_client:
            active_client.close()
    return report
