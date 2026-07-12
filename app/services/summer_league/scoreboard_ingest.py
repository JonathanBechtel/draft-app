"""Job B step 0 — Summer League scoreboard/schedule ingest.

Fetches the active event's full Summer League schedule from the
stats.nba.com schedule feed and upserts ``tip_datetime`` + honest status
(coarse enum + raw text) + raw provider team IDs/links + non-null scores onto
``summer_league_games`` **before** normalization or any Desk projection runs
(`docs/plans/summer-league-scouts-desk-behavior-spec.md` §2 "Resolution & data
prerequisites", §10 Job B step 0). The Morning Card and the Ledger->Morning
state-machine flip cannot exist without tip times, so this step runs first in
every tick. #529 widened this from a rolling today/tomorrow window to the
full active-event schedule so this doubles as a complete canonical schedule
source, not just a state-machine anchor -- the date window is now opt-in via
``target_dates`` for tests/callers that want a narrower slice.

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
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import Event
from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueGameStatus,
    SummerLeagueTeamEntry,
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

# Substrings (already lower-cased) that mark a game as postponed/canceled in
# ``gameStatusText``. "ppd" is a confirmed real value captured from the NBA
# Stats schedule feed (LeagueID 15, 2021 season, game 1522100005 -- rained
# out during Las Vegas Summer League); "postpon"/"cancel" are defensive
# variants for the same real-world condition. Checked *before* the numeric
# ``gameStatus`` code below so a feed that briefly reports a stale/live code
# alongside postponed/canceled text is never classified as live. Kept as one
# combined tuple (rather than two disjoint ones) since callers outside this
# module (``event_desk.registry._to_generic_status``) still need a single
# "is this postponed-or-canceled at all" check against ``status_text`` for
# legacy rows persisted before the POSTPONED/CANCELED statuses existed
# (fix #4) -- ``_POSTPONED_MARKERS``/``_CANCELED_MARKERS`` below only decide
# which of the two *new* statuses a freshly-ingested row gets.
_POSTPONED_OR_CANCELED_MARKERS = ("ppd", "postpon", "cancel")

# Disjoint marker subsets deciding which terminal status a freshly-ingested
# row gets (fix #4): "cancel" text -> CANCELED, "ppd"/"postpon" -> POSTPONED.
_POSTPONED_MARKERS = ("ppd", "postpon")
_CANCELED_MARKERS = ("cancel",)


@dataclass(frozen=True)
class ScoreboardGame:
    """One parsed scoreboard/schedule game row."""

    nba_stats_game_id: str
    game_date: date | None
    tip_datetime: datetime | None
    status: SummerLeagueGameStatus
    # Honest raw ``gameStatusText`` (e.g. "Final/OT", "PPD"), retained
    # alongside the coarse ``status`` enum bucket.
    status_text: str | None = None
    # Raw NBA Stats provider team IDs (schedule feed's homeTeam.teamId /
    # awayTeam.teamId, stringified). Resolved against
    # ``summer_league_team_entries`` in a single batch query per competition
    # by :func:`upsert_scoreboard_games`.
    home_nba_stats_team_id: str | None = None
    away_nba_stats_team_id: str | None = None
    home_score: int | None = None
    away_score: int | None = None


@dataclass
class ScoreboardIngestReport:
    """Summary of one scoreboard-ingest run across every resolved competition."""

    competitions_checked: int = 0
    games_seen: int = 0
    games_created: int = 0
    games_updated: int = 0
    errors: list[str] = field(default_factory=list)
    # Provider team IDs (schedule feed's teamId, stringified) seen on a game
    # but not resolvable to an existing ``summer_league_team_entries`` row
    # for that competition. Reported rather than fabricated as a new row --
    # team entries are owned by roster/normalization ingest, not this step.
    unresolved_team_ids: list[str] = field(default_factory=list)


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
        ``SCHEDULED`` / ``IN_PROGRESS`` / ``FINAL`` / ``POSTPONED`` /
        ``CANCELED`` / ``UNKNOWN``.
    """
    text = (game_status_text or "").strip().lower()

    # Postponed/canceled text wins over the numeric code, checked first: a
    # postponed game is never live, regardless of what (possibly stale)
    # ``gameStatus`` code the feed is currently reporting alongside it.
    # Fix #4: persist the real terminal status instead of collapsing it to
    # SCHEDULED, so every downstream consumer that filters on this column
    # (`live_ingestion._LIVE_STATUSES`, `normalization.resolve_game_status`)
    # excludes postponed/canceled games automatically rather than
    # re-deriving from ``status_text``.
    if any(marker in text for marker in _CANCELED_MARKERS):
        return SummerLeagueGameStatus.CANCELED
    if any(marker in text for marker in _POSTPONED_MARKERS):
        return SummerLeagueGameStatus.POSTPONED

    code: int | None = None
    if isinstance(game_status, bool):
        code = None
    elif isinstance(game_status, int):
        code = game_status
    elif isinstance(game_status, str) and game_status.strip().lstrip("-").isdigit():
        code = int(game_status.strip())

    if code is not None and code in _STATUS_CODE_MAP:
        return _STATUS_CODE_MAP[code]

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


def _team_id_or_none(value: Any) -> str | None:
    """Stringify a raw provider ``teamId``, or ``None`` when absent/blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _score_or_none(value: Any) -> int | None:
    """Parse a raw provider team score, treating ``0``/missing as "not yet scored".

    The schedule feed reports ``0`` for both teams on every game that hasn't
    tipped off yet -- indistinguishable from a real 0 (which never happens in
    basketball). Normalizing both to ``None`` here means callers can safely
    apply "only overwrite when not ``None``" update semantics without a
    scheduled game's placeholder zero ever clobbering a real score.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value or None
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        parsed = int(value.strip())
        return parsed or None
    return None


def parse_scoreboard_games(
    payload: Mapping[str, Any], *, target_dates: Iterable[date] | None = None
) -> list[ScoreboardGame]:
    """Parse a ``scheduleleaguev2`` payload into :class:`ScoreboardGame` rows.

    Args:
        payload: Parsed NBA Stats ``scheduleleaguev2`` JSON response.
        target_dates: Optional calendar dates to keep. When ``None`` (the
            production default -- see :func:`run_scoreboard_ingest`), every
            game with a resolvable schedule date is kept, i.e. the full
            known schedule for whichever competition the payload came from.
            Tests/callers that want a narrower slice (e.g. "just today and
            tomorrow") pass an explicit set.

    Returns:
        One :class:`ScoreboardGame` per kept game, in payload order.
    """
    wanted = set(target_dates) if target_dates is not None else None
    schedule = payload.get("leagueSchedule") or {}
    games: list[ScoreboardGame] = []
    for game_date_block in schedule.get("gameDates") or []:
        for game in game_date_block.get("games") or []:
            game_id = str(game.get("gameId") or "").strip()
            if not game_id:
                continue
            tip_datetime = parse_tip_datetime_utc(game)
            game_date = _parse_game_date(game, tip_datetime)
            if game_date is None:
                continue
            if wanted is not None and game_date not in wanted:
                continue
            status_text_raw = game.get("gameStatusText")
            status = map_game_status(game.get("gameStatus"), status_text_raw)
            home_team = game.get("homeTeam") or {}
            away_team = game.get("awayTeam") or {}
            games.append(
                ScoreboardGame(
                    nba_stats_game_id=game_id,
                    game_date=game_date,
                    tip_datetime=tip_datetime,
                    status=status,
                    status_text=(
                        status_text_raw.strip()
                        if isinstance(status_text_raw, str) and status_text_raw.strip()
                        else None
                    ),
                    home_nba_stats_team_id=_team_id_or_none(home_team.get("teamId")),
                    away_nba_stats_team_id=_team_id_or_none(away_team.get("teamId")),
                    home_score=_score_or_none(home_team.get("score")),
                    away_score=_score_or_none(away_team.get("score")),
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


async def _teams_by_source_id(
    db: AsyncSession, competition_id: int
) -> dict[str, SummerLeagueTeamEntry]:
    """Batch-load one competition's team entries, keyed by ``nba_stats_team_id``.

    One query per competition per :func:`upsert_scoreboard_games` call --
    resolving every game's home/away provider team ID against this in-memory
    map, rather than a per-game lookup query, is what keeps team resolution
    from being N+1.
    """
    result = await db.execute(
        select(SummerLeagueTeamEntry).where(
            SummerLeagueTeamEntry.competition_id == competition_id  # type: ignore[arg-type]
        )
    )
    return {team.nba_stats_team_id: team for team in result.scalars().all()}


async def upsert_scoreboard_games(
    db: AsyncSession, *, competition_id: int, games: Iterable[ScoreboardGame]
) -> tuple[int, int, list[str]]:
    """Create or update ``summer_league_games`` rows from parsed scoreboard games.

    Resolves every game's raw provider team IDs against this competition's
    ``summer_league_team_entries`` in a single batch query
    (:func:`_teams_by_source_id`) and links ``home_team_entry_id`` /
    ``away_team_entry_id`` when resolved. An unresolved provider team ID is
    reported (returned, not fabricated as a new team-entry row) -- team
    entries are owned by roster/normalization ingest.

    Missing/zero fields never overwrite canonical values: an update only
    touches ``home_team_entry_id`` / ``away_team_entry_id`` / ``home_score`` /
    ``away_score`` when the freshly-parsed value is present, so a
    not-yet-tipped or unresolved re-poll can never null out or zero a value
    a previous poll (or normalization) already set.

    Args:
        db: Async database session (caller controls the transaction/commit).
        competition_id: The competition new rows should be created under.
        games: Parsed scoreboard games, e.g. from :func:`parse_scoreboard_games`.

    Returns:
        ``(created, updated, unresolved_team_ids)`` -- row counts plus any
        provider team IDs seen but not found in this competition's team
        entries.
    """
    created = 0
    updated = 0
    unresolved_team_ids: list[str] = []
    teams_by_source_id = await _teams_by_source_id(db, competition_id)

    for game in games:
        stmt = select(SummerLeagueGame).where(
            SummerLeagueGame.nba_stats_game_id == game.nba_stats_game_id  # type: ignore[arg-type]
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()
        tip_datetime = _to_naive_utc(game.tip_datetime)

        home_team = (
            teams_by_source_id.get(game.home_nba_stats_team_id)
            if game.home_nba_stats_team_id
            else None
        )
        away_team = (
            teams_by_source_id.get(game.away_nba_stats_team_id)
            if game.away_nba_stats_team_id
            else None
        )
        if game.home_nba_stats_team_id and home_team is None:
            unresolved_team_ids.append(game.home_nba_stats_team_id)
        if game.away_nba_stats_team_id and away_team is None:
            unresolved_team_ids.append(game.away_nba_stats_team_id)

        if existing is None:
            db.add(
                SummerLeagueGame(
                    competition_id=competition_id,
                    nba_stats_game_id=game.nba_stats_game_id,
                    game_date=game.game_date,
                    tip_datetime=tip_datetime,
                    status=game.status,
                    status_text=game.status_text,
                    home_nba_stats_team_id=game.home_nba_stats_team_id,
                    away_nba_stats_team_id=game.away_nba_stats_team_id,
                    home_team_entry_id=home_team.id if home_team else None,
                    away_team_entry_id=away_team.id if away_team else None,
                    home_score=game.home_score,
                    away_score=game.away_score,
                )
            )
            created += 1
        else:
            if game.game_date is not None:
                existing.game_date = game.game_date
            if tip_datetime is not None:
                existing.tip_datetime = tip_datetime
            existing.status = game.status
            if game.status_text is not None:
                existing.status_text = game.status_text
            if game.home_nba_stats_team_id is not None:
                existing.home_nba_stats_team_id = game.home_nba_stats_team_id
            if game.away_nba_stats_team_id is not None:
                existing.away_nba_stats_team_id = game.away_nba_stats_team_id
            if home_team is not None:
                existing.home_team_entry_id = home_team.id
            if away_team is not None:
                existing.away_team_entry_id = away_team.id
            if game.home_score is not None:
                existing.home_score = game.home_score
            if game.away_score is not None:
                existing.away_score = game.away_score
            updated += 1
    await db.flush()
    return created, updated, unresolved_team_ids


async def run_scoreboard_ingest(
    db: AsyncSession,
    *,
    today: date | None = None,
    target_dates: Iterable[date] | None = None,
    client: NBAStatsClient | None = None,
) -> ScoreboardIngestReport:
    """Job B step 0 -- fetch and upsert the active event's SL schedule.

    Resolves the active competition(s) (:func:`resolve_target_competitions`),
    fetches each one's ``scheduleleaguev2`` feed via the shared ``curl_cffi``
    :class:`NBAStatsClient`, maps NBA Stats status codes to
    :class:`SummerLeagueGameStatus`, and upserts ``tip_datetime`` / ``status``
    / raw provider team IDs / scores onto ``summer_league_games`` -- before
    normalization or the rest of the desk tick runs (behavior spec §10 Job B
    step 0). The caller owns the session commit, matching every other Summer
    League ingest step in this package.

    By default (``target_dates=None``) every known game for each resolved
    competition is retained -- the schedule feed already scopes each fetch to
    one competition's own tournament window, so this keeps the full active
    event's schedule as the canonical source (#529), including games more
    than a day or two out. Pass an explicit ``target_dates`` to narrow to a
    specific window (tests, or a caller that only wants a slice).

    Args:
        db: Async database session (caller controls the transaction).
        today: Override for "today" (tests only); defaults to the current UTC
            date. Only used for the competition year-fallback in
            :func:`resolve_target_competitions`.
        target_dates: Optional calendar dates to restrict ingested games to.
            Omit for the production full-schedule default.
        client: Optional injected :class:`NBAStatsClient` (tests only); when
            omitted a real client is opened for the duration of the run and
            closed afterward.

    Returns:
        Aggregate counts across every resolved competition, plus any
        per-competition fetch errors (a failed competition does not abort the
        others) and any unresolved provider team IDs.
    """
    resolved_today = today or datetime.now(timezone.utc).date()

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
            created, updated, unresolved_team_ids = await upsert_scoreboard_games(
                db, competition_id=competition.id, games=games
            )
            report.games_created += created
            report.games_updated += updated
            report.unresolved_team_ids.extend(unresolved_team_ids)
    finally:
        if owns_client:
            active_client.close()
    return report
