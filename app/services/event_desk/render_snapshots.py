"""Typed codec + repository for `EventDeskRenderSnapshot` (launch-readiness item 9).

Two concerns live here, deliberately kept in one small module rather than split across
a "codec" file and a "repository" file, because neither is useful without the other:

* **Codec** -- pure functions that turn a
  :class:`~app.services.summer_league.desk_read.DeskView` into JSON-safe dicts
  (`serialize_desk_view`) and back (`deserialize_desk_view`). No I/O, no `AsyncSession`.
  A schema-version mismatch on read raises :class:`UnsupportedRenderSnapshotSchemaVersion`
  rather than guessing at a shape the codec no longer understands.
* **Repository** -- `upsert_render_snapshots` (batch write) and `get_render_snapshot`
  (exact read), both taking an `AsyncSession` as the first parameter and never
  committing (CLAUDE.md service-layer convention -- the caller/tick owns the transaction).

`DeskView` (not just `DeskPayload`) is what gets persisted: a cold read needs the
player/matchup/tracker-team view-context enrichment too, or the request would still have
to run `get_desk_view_context`'s batched-but-nonzero queries on every hit. Persisting the
full `DeskView` is what makes a snapshot read a single row fetch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.event_desk import EventDailyState
from app.schemas.event_desk_render_snapshot import EventDeskRenderSnapshot
from app.services.event_desk.payload import (
    DeskFreshness,
    DeskHero,
    DeskHeroLine,
    DeskLedgerRow,
    DeskLiveBoardRow,
    DeskPayload,
    DeskSlateRow,
    DeskTrackerRow,
    DeskTrackerSection,
)
from app.services.summer_league.desk_read import DeskView

# JSON-safe dict alias used throughout this module's codec functions -- `Any` (not a
# recursive JSON type) deliberately, so assigning a decoded value back onto a typed
# dataclass field (e.g. `str`, `Optional[datetime]`) never needs a per-call cast.
JsonDict = dict[str, Any]

# Bump when a `DeskPayload`/`DeskView` field is added, removed, or reshaped in a way the
# codec below can't decode losslessly. Persisted rows carry the version they were written
# with; `deserialize_desk_view` rejects anything it doesn't recognize instead of guessing.
CURRENT_SCHEMA_VERSION = 1


class UnsupportedRenderSnapshotSchemaVersion(ValueError):
    """A persisted snapshot's `schema_version` isn't one this build's codec understands."""

    def __init__(self, schema_version: int) -> None:
        self.schema_version = schema_version
        super().__init__(
            "Unsupported EventDeskRenderSnapshot schema_version: "
            f"{schema_version!r} (this build's codec understands "
            f"{CURRENT_SCHEMA_VERSION!r})"
        )


# --------------------------------------------------------------------------- #
# Scalar helpers
# --------------------------------------------------------------------------- #
def _dt_to_iso(value: Optional[datetime]) -> Optional[str]:
    """Encode a naive UTC datetime as ISO-8601, preserving `None`."""
    return value.isoformat() if value is not None else None


def _iso_to_dt(value: Optional[str]) -> Optional[datetime]:
    """Decode an ISO-8601 string back to a datetime, preserving `None`."""
    return datetime.fromisoformat(value) if value is not None else None


def _id_keyed_dict_to_json(data: dict[int, Any]) -> JsonDict:
    """Encode an `{int: ...}` lookup as JSON-safe `{str: ...}` (JSON has no int keys)."""
    return {str(key): value for key, value in data.items()}


def _id_keyed_dict_from_json(data: Optional[JsonDict]) -> dict[int, Any]:
    """Decode a `{str: ...}` JSON lookup back to `{int: ...}`, defaulting to empty."""
    return {int(key): value for key, value in (data or {}).items()}


# --------------------------------------------------------------------------- #
# DeskPayload field codecs
# --------------------------------------------------------------------------- #
def _serialize_freshness(freshness: DeskFreshness) -> JsonDict:
    return {
        "last_tick_at": _dt_to_iso(freshness.last_tick_at),
        "next_tick_eta": _dt_to_iso(freshness.next_tick_eta),
        "as_of_et_label": freshness.as_of_et_label,
    }


def _deserialize_freshness(data: JsonDict) -> DeskFreshness:
    return DeskFreshness(
        last_tick_at=_iso_to_dt(data["last_tick_at"]),
        next_tick_eta=_iso_to_dt(data["next_tick_eta"]),
        as_of_et_label=data["as_of_et_label"],
    )


def _serialize_hero_line(line: Optional[DeskHeroLine]) -> Optional[JsonDict]:
    if line is None:
        return None
    return {"pts": line.pts, "reb": line.reb, "ast": line.ast, "gmsc": line.gmsc}


def _deserialize_hero_line(data: Optional[JsonDict]) -> Optional[DeskHeroLine]:
    if data is None:
        return None
    return DeskHeroLine(
        pts=data["pts"], reb=data["reb"], ast=data["ast"], gmsc=data["gmsc"]
    )


def _serialize_hero(hero: DeskHero) -> JsonDict:
    return {
        "kind": hero.kind,
        "game_id": hero.game_id,
        "subject_player_id": hero.subject_player_id,
        "subject_player_id_2": hero.subject_player_id_2,
        "headline": hero.headline,
        "tagline": hero.tagline,
        "facts": list(hero.facts),
        "subject_line": _serialize_hero_line(hero.subject_line),
        "subject_line_2": _serialize_hero_line(hero.subject_line_2),
    }


def _deserialize_hero(data: JsonDict) -> DeskHero:
    return DeskHero(
        kind=data["kind"],
        game_id=data["game_id"],
        subject_player_id=data["subject_player_id"],
        subject_player_id_2=data["subject_player_id_2"],
        headline=data["headline"],
        tagline=data["tagline"],
        facts=list(data.get("facts") or []),
        # `.get(...)` (not `data[...]`) -- a schema_version=1 row persisted
        # before #541 has neither key; both decode to `None` (the same
        # default a pre-#541 `DeskHero` had), never a KeyError.
        subject_line=_deserialize_hero_line(data.get("subject_line")),
        subject_line_2=_deserialize_hero_line(data.get("subject_line_2")),
    )


def _serialize_slate_row(row: DeskSlateRow) -> JsonDict:
    return {
        "game_id": row.game_id,
        "matchup_label": row.matchup_label,
        "status": row.status,
        "tip_datetime": _dt_to_iso(row.tip_datetime),
        "weight": row.weight,
        "read": row.read,
    }


def _deserialize_slate_row(data: JsonDict) -> DeskSlateRow:
    return DeskSlateRow(
        game_id=data["game_id"],
        matchup_label=data["matchup_label"],
        status=data["status"],
        tip_datetime=_iso_to_dt(data["tip_datetime"]),
        weight=data["weight"],
        read=data["read"],
    )


def _serialize_live_board_row(row: DeskLiveBoardRow) -> JsonDict:
    return {
        "game_id": row.game_id,
        "matchup_label": row.matchup_label,
        "status": row.status,
        "home_score": row.home_score,
        "away_score": row.away_score,
        "top_performer_player_id": row.top_performer_player_id,
        "top_performer_gmsc": row.top_performer_gmsc,
        "read": row.read,
    }


def _deserialize_live_board_row(data: JsonDict) -> DeskLiveBoardRow:
    return DeskLiveBoardRow(
        game_id=data["game_id"],
        matchup_label=data["matchup_label"],
        status=data["status"],
        home_score=data["home_score"],
        away_score=data["away_score"],
        top_performer_player_id=data["top_performer_player_id"],
        top_performer_gmsc=data["top_performer_gmsc"],
        read=data["read"],
    )


def _serialize_ledger_row(row: DeskLedgerRow) -> JsonDict:
    return {
        "game_id": row.game_id,
        "player_id": row.player_id,
        "gmsc": row.gmsc,
        "pctl": row.pctl,
        "grade": row.grade,
        "read": row.read,
    }


def _deserialize_ledger_row(data: JsonDict) -> DeskLedgerRow:
    return DeskLedgerRow(
        game_id=data["game_id"],
        player_id=data["player_id"],
        gmsc=data["gmsc"],
        pctl=data["pctl"],
        grade=data["grade"],
        read=data["read"],
    )


def _serialize_tracker_row(row: DeskTrackerRow) -> JsonDict:
    return {
        "player_id": row.player_id,
        "display_name": row.display_name,
        "identity_label": row.identity_label,
        "gp": row.gp,
        "minutes": row.minutes,
        "gmsc": row.gmsc,
        "grade": row.grade,
        "stat_columns": dict(row.stat_columns),
    }


def _deserialize_tracker_row(data: JsonDict) -> DeskTrackerRow:
    return DeskTrackerRow(
        player_id=data["player_id"],
        display_name=data["display_name"],
        identity_label=data["identity_label"],
        gp=data["gp"],
        minutes=data["minutes"],
        gmsc=data["gmsc"],
        grade=data["grade"],
        stat_columns=dict(data.get("stat_columns") or {}),
    )


def _serialize_tracker_section(section: DeskTrackerSection) -> JsonDict:
    return {
        "cohort": section.cohort,
        "stat_view": section.stat_view,
        "rows": [_serialize_tracker_row(row) for row in section.rows],
        "truncated": section.truncated,
    }


def _deserialize_tracker_section(data: JsonDict) -> DeskTrackerSection:
    return DeskTrackerSection(
        cohort=data["cohort"],
        stat_view=data["stat_view"],
        rows=[_deserialize_tracker_row(row) for row in data.get("rows") or []],
        truncated=data["truncated"],
    )


def serialize_desk_payload(payload: DeskPayload) -> JsonDict:
    """Encode a `DeskPayload` to a JSON-safe dict, preserving field order and nulls."""
    return {
        "daily_state": payload.daily_state,
        "is_home_owner": payload.is_home_owner,
        "hero": _serialize_hero(payload.hero),
        "slate": [_serialize_slate_row(row) for row in payload.slate],
        "live_board": [_serialize_live_board_row(row) for row in payload.live_board],
        "ledger": [_serialize_ledger_row(row) for row in payload.ledger],
        "tracker": _serialize_tracker_section(payload.tracker),
        "freshness": _serialize_freshness(payload.freshness),
    }


def deserialize_desk_payload(data: JsonDict) -> DeskPayload:
    """Decode a JSON-safe dict back into a `DeskPayload`. Assumes `CURRENT_SCHEMA_VERSION`.

    Callers that read a persisted row must check `schema_version` themselves (or go
    through `deserialize_desk_view`, which does) before calling this.
    """
    return DeskPayload(
        daily_state=data["daily_state"],
        is_home_owner=data["is_home_owner"],
        hero=_deserialize_hero(data["hero"]),
        slate=[_deserialize_slate_row(row) for row in data.get("slate") or []],
        live_board=[
            _deserialize_live_board_row(row) for row in data.get("live_board") or []
        ],
        ledger=[_deserialize_ledger_row(row) for row in data.get("ledger") or []],
        tracker=_deserialize_tracker_section(data["tracker"]),
        freshness=_deserialize_freshness(data["freshness"]),
    )


# --------------------------------------------------------------------------- #
# View-context codec
# --------------------------------------------------------------------------- #
def serialize_view_context(
    *,
    players: dict[int, dict[str, Any]],
    matchups: dict[int, dict[str, Any]],
    tracker_teams: dict[int, dict[str, Optional[str]]],
) -> JsonDict:
    """Encode `DeskView`'s player/matchup/tracker-team enrichment dicts as JSON-safe."""
    return {
        "players": _id_keyed_dict_to_json(players),
        "matchups": _id_keyed_dict_to_json(matchups),
        "tracker_teams": _id_keyed_dict_to_json(tracker_teams),
    }


def deserialize_view_context(
    data: JsonDict,
) -> tuple[
    dict[int, dict[str, Any]],
    dict[int, dict[str, Any]],
    dict[int, dict[str, Optional[str]]],
]:
    """Decode the JSON-safe view-context dict back to `{int: ...}`-keyed lookups."""
    return (
        _id_keyed_dict_from_json(data.get("players")),
        _id_keyed_dict_from_json(data.get("matchups")),
        _id_keyed_dict_from_json(data.get("tracker_teams")),
    )


# --------------------------------------------------------------------------- #
# DeskView codec (public entry points)
# --------------------------------------------------------------------------- #
def serialize_desk_view(view: DeskView) -> tuple[Optional[JsonDict], JsonDict]:
    """Encode a `DeskView` into `(payload_json, view_context_json)` for persistence.

    `payload_json` is `None` when `view.payload` is `None` (off-window) -- callers
    should not normally persist an off-window snapshot, but the codec stays total
    rather than raising, matching the schema column's nullability.
    """
    payload_json = (
        serialize_desk_payload(view.payload) if view.payload is not None else None
    )
    view_context_json = serialize_view_context(
        players=view.players,
        matchups=view.matchups,
        tracker_teams=view.tracker_teams,
    )
    return payload_json, view_context_json


def deserialize_desk_view(
    *,
    payload_json: Optional[JsonDict],
    view_context_json: JsonDict,
    schema_version: int,
) -> DeskView:
    """Decode a persisted row's JSON columns back into a `DeskView`.

    Args:
        payload_json: The row's `payload_json` column value (may be `None`).
        view_context_json: The row's `view_context_json` column value.
        schema_version: The row's `schema_version` column value.

    Returns:
        The reconstructed `DeskView`, field-for-field equal to what was serialized.

    Raises:
        UnsupportedRenderSnapshotSchemaVersion: `schema_version` isn't
            `CURRENT_SCHEMA_VERSION`.
    """
    if schema_version != CURRENT_SCHEMA_VERSION:
        raise UnsupportedRenderSnapshotSchemaVersion(schema_version)

    payload = (
        deserialize_desk_payload(payload_json) if payload_json is not None else None
    )
    players, matchups, tracker_teams = deserialize_view_context(view_context_json)
    return DeskView(
        payload=payload,
        players=players,
        matchups=matchups,
        tracker_teams=tracker_teams,
    )


# --------------------------------------------------------------------------- #
# Repository operations
# --------------------------------------------------------------------------- #
_CONFLICT_INDEX_ELEMENTS = (
    "event_id",
    "daily_state",
    "tracker_cohort",
    "tracker_stat_view",
)


@dataclass(frozen=True)
class RenderSnapshotWrite:
    """One (event, daily_state, tracker_cohort, tracker_stat_view) variant to upsert."""

    event_id: int
    daily_state: EventDailyState
    tracker_cohort: str
    tracker_stat_view: str
    view: DeskView
    source_freshness_tick_at: Optional[datetime] = None
    source_freshness_next_tick_eta: Optional[datetime] = None


async def upsert_render_snapshots(
    db: AsyncSession,
    writes: Sequence[RenderSnapshotWrite],
    *,
    now: Optional[datetime] = None,
) -> None:
    """Batch-upsert render snapshot variants in ONE bounded SQL statement.

    All supported variants coexist and update independently: each row is keyed by its
    own `(event_id, daily_state, tracker_cohort, tracker_stat_view)`, so upserting a
    Live-state row never touches a Preview/Recap row for the same event. The number of
    statements this issues is fixed at one (a single multi-row `INSERT ... ON CONFLICT
    DO UPDATE`) regardless of how many writes are batched -- it does not grow into a
    per-row round trip.

    Never commits (caller/tick controls the transaction). A no-op (zero statements)
    when `writes` is empty.

    Args:
        db: Active database session.
        writes: The variants to upsert; may be any length, including empty.
        now: Override for `updated_at` (tests; defaults to the current UTC instant).
    """
    if not writes:
        return

    resolved_now = (
        now if now is not None else datetime.now(timezone.utc).replace(tzinfo=None)
    )
    values: list[dict[str, object]] = []
    for write in writes:
        payload_json, view_context_json = serialize_desk_view(write.view)
        values.append(
            {
                "event_id": write.event_id,
                "daily_state": write.daily_state,
                "tracker_cohort": write.tracker_cohort,
                "tracker_stat_view": write.tracker_stat_view,
                "schema_version": CURRENT_SCHEMA_VERSION,
                "payload_json": payload_json,
                "view_context_json": view_context_json,
                "source_freshness_tick_at": write.source_freshness_tick_at,
                "source_freshness_next_tick_eta": write.source_freshness_next_tick_eta,
                "updated_at": resolved_now,
            }
        )

    stmt = insert(EventDeskRenderSnapshot).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=_CONFLICT_INDEX_ELEMENTS,
        set_={
            "schema_version": stmt.excluded.schema_version,
            "payload_json": stmt.excluded.payload_json,
            "view_context_json": stmt.excluded.view_context_json,
            "source_freshness_tick_at": stmt.excluded.source_freshness_tick_at,
            "source_freshness_next_tick_eta": stmt.excluded.source_freshness_next_tick_eta,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await db.execute(stmt)


async def get_render_snapshot(
    db: AsyncSession,
    *,
    event_id: int,
    daily_state: EventDailyState,
    tracker_cohort: str,
    tracker_stat_view: str,
) -> Optional[EventDeskRenderSnapshot]:
    """Exact single-row read by the full `(event, daily_state, cohort, stat_view)` key.

    One indexed lookup against `uq_event_desk_render_snapshots_variant` -- the fast
    path a cold request-time read is meant to take instead of reassembling a `DeskView`.

    Args:
        db: Active database session.
        event_id: The registered `events.id` to read a snapshot for.
        daily_state: Which materialized variant (Preview / Live / Recap).
        tracker_cohort: The Class Tracker cohort toggle state.
        tracker_stat_view: The Class Tracker stat-view toggle state.

    Returns:
        The matching row, or `None` if no snapshot has been materialized yet for this
        exact variant.
    """
    stmt = select(EventDeskRenderSnapshot).where(
        EventDeskRenderSnapshot.event_id == event_id,  # type: ignore[arg-type]
        EventDeskRenderSnapshot.daily_state == daily_state,  # type: ignore[arg-type]
        EventDeskRenderSnapshot.tracker_cohort == tracker_cohort,  # type: ignore[arg-type]
        EventDeskRenderSnapshot.tracker_stat_view == tracker_stat_view,  # type: ignore[arg-type]
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "JsonDict",
    "RenderSnapshotWrite",
    "UnsupportedRenderSnapshotSchemaVersion",
    "deserialize_desk_payload",
    "deserialize_desk_view",
    "deserialize_view_context",
    "get_render_snapshot",
    "serialize_desk_payload",
    "serialize_desk_view",
    "serialize_view_context",
    "upsert_render_snapshots",
]
