"""Stable content watermark for Summer League metrics rebuild inputs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayByPlayEvent,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourceDocument,
    SummerLeagueSourceRecord,
    SummerLeagueTeamGameLog,
)
from app.schemas.summer_league_events import SummerLeagueShotEvent

# Bump when the set or interpretation of metric inputs changes. This makes a
# deployment that changes watermark semantics force one correct full rebuild.
METRICS_INPUT_WATERMARK_VERSION = "sl-metrics-input-v2"

_IMPLEMENTATION_FILES = (
    ("metrics.py", Path(__file__).with_name("metrics.py")),
    ("scoped_metrics.py", Path(__file__).with_name("scoped_metrics.py")),
    ("metric_publish.py", Path(__file__).with_name("metric_publish.py")),
    (
        "schemas/summer_league_metrics.py",
        Path(__file__).parents[2] / "schemas" / "summer_league_metrics.py",
    ),
)


def _implementation_fingerprint() -> str:
    """Hash every module and version constant that affects metric values."""
    digest = hashlib.sha256()
    for label, path in _IMPLEMENTATION_FILES:
        digest.update(label.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


METRICS_IMPLEMENTATION_FINGERPRINT = _implementation_fingerprint()

_VOLATILE_COLUMNS = frozenset({"created_at", "updated_at"})


def _json_value(value: object) -> object:
    """Convert database values into deterministic JSON-compatible scalars."""
    if isinstance(value, Enum):
        return value.value
    return value


def _add_rows(
    digest: Any,
    *,
    label: str,
    rows: Iterable[Any],
) -> None:
    """Add one ordered input relation to ``digest`` deterministically."""
    digest.update(label.encode())
    digest.update(b"\0")
    for row in rows:
        payload = json.dumps(
            [_json_value(value) for value in row],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        digest.update(payload.encode())
        digest.update(b"\n")


def _table_content_hash(model: Any) -> Any:
    """Build a commutative Postgres hash over every stable table column.

    The aggregate intentionally excludes ``created_at`` and ``updated_at``:
    those columns are bookkeeping and routine normalization can touch them
    without changing the values consumed by ``metrics.compute``. The row id
    remains part of the payload, so inserts and deletes change the aggregate
    as well as edits to any metric input.
    """
    columns = tuple(
        column
        for column in model.__table__.columns
        if column.name not in _VOLATILE_COLUMNS
    )
    row_payload = func.json_build_array(*columns)
    row_hash = func.hashtextextended(cast(row_payload, String), 0)
    # PostgreSQL returns NUMERIC for SUM(bigint); cast it back to text so the
    # result remains JSON-serializable for the common watermark row helper.
    return cast(func.coalesce(func.sum(row_hash), 0), String)


async def _add_table_content_summary(
    db: AsyncSession,
    digest: Any,
    *,
    label: str,
    model: Any,
) -> None:
    """Add a one-row count/hash summary for a high-volume input table."""
    result = await db.execute(select(func.count(), _table_content_hash(model)))
    _add_rows(digest, label=label, rows=result.all())


async def calculate_metrics_input_watermark(db: AsyncSession) -> str:
    """Hash stable source content that can affect a full metrics rebuild.

    Raw-file content hashes capture box, shot, and play-by-play changes without
    being fooled by idempotent normalization updating ``updated_at``. Canonical
    player mappings and game state are included separately because resolution
    and scoreboard ingest can change metric eligibility without changing a raw
    file. Competition identity covers the year/venue dimensions persisted by
    the rebuild. All normalized metric fact tables are included as one-row
    database aggregates so their values are covered without transferring every
    high-volume row into the hourly runner.
    """
    digest = hashlib.sha256()
    digest.update(METRICS_INPUT_WATERMARK_VERSION.encode())
    digest.update(b"\n")
    digest.update(METRICS_IMPLEMENTATION_FINGERPRINT.encode())
    digest.update(b"\n")

    raw_files = (
        await db.execute(
            select(  # type: ignore[call-overload]
                SummerLeagueSourceDocument.relative_path,
                SummerLeagueSourceDocument.sha256,
                SummerLeagueSourceDocument.parse_status,
                SummerLeagueSourceDocument.row_count,
            ).order_by(SummerLeagueSourceDocument.relative_path)
        )
    ).all()
    _add_rows(digest, label="raw_files", rows=raw_files)

    source_players = (
        await db.execute(
            select(  # type: ignore[call-overload]
                SummerLeagueSourceRecord.nba_stats_person_id,
                SummerLeagueSourceRecord.canonical_player_id,
                SummerLeagueSourceRecord.resolution_status,
            ).order_by(SummerLeagueSourceRecord.nba_stats_person_id)
        )
    ).all()
    _add_rows(digest, label="source_players", rows=source_players)

    games = (
        await db.execute(
            select(  # type: ignore[call-overload,misc]
                SummerLeagueGame.nba_stats_game_id,
                SummerLeagueGame.status,
                SummerLeagueGame.game_date,
                SummerLeagueGame.tip_datetime,
                SummerLeagueGame.home_team_entry_id,
                SummerLeagueGame.away_team_entry_id,
                SummerLeagueGame.home_score,
                SummerLeagueGame.away_score,
            ).order_by(SummerLeagueGame.nba_stats_game_id)
        )
    ).all()
    _add_rows(digest, label="games", rows=games)

    competitions = (
        await db.execute(
            select(  # type: ignore[call-overload]
                SummerLeagueEdition.id,
                SummerLeagueEdition.year,
                SummerLeagueEdition.venue_slug,
            ).order_by(SummerLeagueEdition.id)
        )
    ).all()
    _add_rows(digest, label="competitions", rows=competitions)

    await _add_table_content_summary(
        db,
        digest,
        label="player_game_logs",
        model=SummerLeaguePlayerGameLog,
    )
    await _add_table_content_summary(
        db,
        digest,
        label="shot_events",
        model=SummerLeagueShotEvent,
    )
    await _add_table_content_summary(
        db,
        digest,
        label="team_game_logs",
        model=SummerLeagueTeamGameLog,
    )
    await _add_table_content_summary(
        db,
        digest,
        label="play_by_play_events",
        model=SummerLeaguePlayByPlayEvent,
    )
    return digest.hexdigest()
