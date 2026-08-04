"""Engine possession adapters for Summer League read-side game rows.

The shared stat engine owns the possession formula. Read-side services only
need to map their source-specific game-log rows into the neutral
``StatInputs`` shape and apply the player-minute share at the same game grain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Optional, Sequence

from app.services.sources.summer_league.metrics import MIN_COMPLETE_TEAM_MP
from app.services.stats.formulas import pace_seconds_from_possessions
from app.services.stats.inputs import BOX_INT_FIELDS, StatInputs


def team_box_columns(team_log: Any, prefix: str) -> list[Any]:
    """Return labelled team-box columns for a read-side game-log query."""
    return [
        team_log.minutes.label(f"{prefix}_mp"),
        *[
            getattr(team_log, field).label(f"{prefix}_{field}")
            for field in BOX_INT_FIELDS
        ],
    ]


def _box_from_row(row: Any, prefix: str) -> StatInputs:
    """Build neutral engine inputs from labelled team-box columns."""
    box = StatInputs(mp=float(getattr(row, f"{prefix}_mp") or 0))
    for field_name in BOX_INT_FIELDS:
        setattr(
            box,
            field_name,
            float(getattr(row, f"{prefix}_{field_name}") or 0),
        )
    return box


def _add_box(target: StatInputs, source: StatInputs) -> None:
    """Add one game box to a competition-grain box total."""
    target.mp += source.mp
    for field_name in BOX_INT_FIELDS:
        setattr(
            target,
            field_name,
            getattr(target, field_name) + getattr(source, field_name),
        )


def _has_complete_game_boxes(row: Any) -> bool:
    """Apply the engine's per-game team-box completeness threshold."""
    team_minutes = float(getattr(row, "team_mp", 0) or 0)
    opponent_minutes = float(getattr(row, "opp_mp", 0) or 0)
    return (
        team_minutes >= MIN_COMPLETE_TEAM_MP
        and opponent_minutes >= MIN_COMPLETE_TEAM_MP
    )


def _competition_records(
    rows: Sequence[Any],
    line_getter: Callable[[Any], Any],
) -> Optional[tuple[list[tuple[Any, Any]], dict[Any, float]]]:
    """Normalize played rows and reject incomplete competition coverage."""
    records: list[tuple[Any, Any]] = []
    seen_games: set[tuple[Any, Any]] = set()
    team_seconds: dict[Any, float] = {}
    for index, row in enumerate(rows):
        line = line_getter(row)
        seconds = float(getattr(line, "minutes_seconds", 0) or 0)
        if seconds <= 0:
            continue
        if not _has_complete_game_boxes(row):
            return None
        team_entry_id = getattr(line, "team_entry_id", None)
        if team_entry_id is None:
            return None
        game_id = getattr(line, "game_id", None)
        if game_id is None:
            game_id = getattr(row, "sl_game_id", None)
        game_key = (game_id if game_id is not None else index, team_entry_id)
        if game_key in seen_games:
            continue
        seen_games.add(game_key)
        records.append((line, row))
        team_seconds[team_entry_id] = team_seconds.get(team_entry_id, 0.0) + seconds
    if not records or not team_seconds:
        return None
    return records, team_seconds


def _pooled_player_possessions(
    records: Sequence[tuple[Any, Any]], team_seconds: dict[Any, float]
) -> Optional[float]:
    """Apply a player's total-minute share to the primary team's pooled boxes."""
    primary_team = max(team_seconds, key=lambda team_id: team_seconds[team_id])
    team = StatInputs()
    opponent = StatInputs()
    player_seconds = 0.0
    for line, row in records:
        player_seconds += float(getattr(line, "minutes_seconds", 0) or 0)
        if getattr(line, "team_entry_id", None) != primary_team:
            continue
        _add_box(team, _box_from_row(row, "team"))
        _add_box(opponent, _box_from_row(row, "opp"))

    team_minutes_fifths = team.mp / 5.0
    team_possessions = team.poss(opponent)
    if team_minutes_fifths <= 0 or team_possessions <= 0 or player_seconds <= 0:
        return None
    return team_possessions * (player_seconds / 60.0) / team_minutes_fifths


def player_possessions_from_row(
    row: Any, *, player_minutes_seconds: Optional[float] = None
) -> Optional[float]:
    """Estimate a player's possessions from same-game team/opponent boxes.

    ``row`` is expected to expose ``minutes_seconds`` plus the labelled
    ``team_*`` and ``opp_*`` columns returned by :func:`team_box_columns`.
    Tests and already-adapted callers may provide ``engine_possessions``
    directly; an explicit ``None`` remains unavailable and never falls back to
    the NBA-supplied player pace field.
    """
    if hasattr(row, "engine_possessions"):
        value = getattr(row, "engine_possessions")
        return float(value) if value is not None and float(value) > 0 else None

    if not _has_complete_game_boxes(row):
        return None

    player_seconds = (
        player_minutes_seconds
        if player_minutes_seconds is not None
        else getattr(row, "minutes_seconds", None)
    )
    if not player_seconds:
        return None

    team = _box_from_row(row, "team")
    opponent = _box_from_row(row, "opp")
    team_minutes_fifths = team.mp / 5.0
    if team_minutes_fifths <= 0:
        return None

    team_possessions = team.poss(opponent)
    if team_possessions <= 0:
        return None
    return team_possessions * (float(player_seconds) / 60.0) / team_minutes_fifths


def player_possessions_from_rows(
    rows: Sequence[Any],
    *,
    line_getter: Optional[Callable[[Any], Any]] = None,
) -> Optional[float]:
    """Estimate possessions at one competition's engine grain.

    The engine first pools the selected team's and opponent's boxes for a
    competition, then applies the player's total minutes share to that pooled
    team possession total. Summing per-game minute shares is not equivalent
    when game pace varies, so callers must pass every played row in the surface
    grain being computed. A competition is unavailable when any played game
    lacks complete team/opponent boxes.

    ``engine_possessions`` is an explicit test seam for DB-free aggregation
    tests; production query rows never expose that attribute.
    """
    if not rows:
        return None

    if all(hasattr(row, "engine_possessions") for row in rows):
        values = [player_possessions_from_row(row) for row in rows]
        if not all(value is not None for value in values):
            return None
        return sum(value for value in values if value is not None)

    get_line = line_getter or (lambda row: row)
    prepared = _competition_records(rows, get_line)
    if prepared is None:
        return None
    return _pooled_player_possessions(*prepared)


@dataclass
class _LeaderCompetitionAggregate:
    """One player's box and engine-possession totals for one competition."""

    slug: Optional[str]
    display_name: Optional[str]
    gp: int = 0
    sec: float = 0.0
    raw_rows: list[Any] = field(default_factory=list)
    totals: dict[str, float] = field(default_factory=dict)

    def add(self, line: Any, raw: Any, counting_fields: Sequence[str]) -> None:
        """Accumulate one played game and its same-game engine estimate."""
        seconds = float(line.minutes_seconds or 0)
        self.gp += 1
        self.sec += seconds
        for key in counting_fields:
            self.totals[key] = self.totals.get(key, 0.0) + float(
                getattr(line, key) or 0
            )
        self.raw_rows.append(raw)

    def complete_pace_seconds(self) -> Optional[float]:
        """Return the per-100 denominator only for complete engine coverage."""
        possessions = player_possessions_from_rows(
            self.raw_rows, line_getter=lambda raw: raw[0]
        )
        if possessions is None:
            return None
        return pace_seconds_from_possessions(possessions)


def aggregate_leader_rows(
    raw_rows: Sequence[Any],
    *,
    counting_fields: Sequence[str],
    min_games: int,
    min_minutes: int,
) -> list[Any]:
    """Aggregate leader query rows without accepting NBA pace as a denominator."""
    by_player: dict[int, dict[int, _LeaderCompetitionAggregate]] = {}
    for raw in raw_rows:
        line = raw[0]
        if line.player_id is None or not line.minutes_seconds:
            continue
        player_comps = by_player.setdefault(line.player_id, {})
        aggregate = player_comps.setdefault(
            line.competition_id,
            _LeaderCompetitionAggregate(
                slug=raw.slug,
                display_name=raw.display_name,
            ),
        )
        aggregate.add(line, raw, counting_fields)

    output: list[Any] = []
    for competitions in by_player.values():
        first = next(iter(competitions.values()))
        gp = sum(c.gp for c in competitions.values())
        sec = sum(c.sec for c in competitions.values())
        if gp < min_games or sec < min_minutes * 60:
            continue

        valid = [
            c for c in competitions.values() if c.complete_pace_seconds() is not None
        ]
        covered_sec = sum(c.sec for c in valid)
        pace_sec = sum(c.complete_pace_seconds() or 0.0 for c in valid)
        if valid and covered_sec < sec:
            # Career/all-venue scopes may combine complete and unavailable
            # competitions. Extrapolate only from complete engine-computed
            # competitions; an individual unavailable competition stays absent.
            pace_sec *= sec / covered_sec

        values = {
            key: sum(c.totals.get(key, 0.0) for c in competitions.values())
            for key in counting_fields
        }
        output.append(
            SimpleNamespace(
                slug=first.slug,
                display_name=first.display_name,
                gp=gp,
                sec=sec,
                pace_sec=pace_sec if valid else 0.0,
                **values,
            )
        )
    return output
