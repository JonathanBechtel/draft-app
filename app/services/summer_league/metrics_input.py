"""Stable content watermark for Summer League metrics rebuild inputs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from enum import Enum
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueCompetition,
    SummerLeagueGame,
    SummerLeagueRawFile,
    SummerLeagueSourcePlayer,
)

# Bump when the set or interpretation of metric inputs changes. This makes a
# deployment that changes watermark semantics force one correct full rebuild.
METRICS_INPUT_WATERMARK_VERSION = "sl-metrics-input-v1"
METRICS_IMPLEMENTATION_FINGERPRINT = hashlib.sha256(
    Path(__file__).with_name("metrics.py").read_bytes()
).hexdigest()


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


async def calculate_metrics_input_watermark(db: AsyncSession) -> str:
    """Hash stable source content that can affect a full metrics rebuild.

    Raw-file content hashes capture box, shot, and play-by-play changes without
    being fooled by idempotent normalization updating ``updated_at``. Canonical
    player mappings and game state are included separately because resolution
    and scoreboard ingest can change metric eligibility without changing a raw
    file. Competition identity covers the year/venue dimensions persisted by
    the rebuild.
    """
    digest = hashlib.sha256()
    digest.update(METRICS_INPUT_WATERMARK_VERSION.encode())
    digest.update(b"\n")
    digest.update(METRICS_IMPLEMENTATION_FINGERPRINT.encode())
    digest.update(b"\n")

    raw_files = (
        await db.execute(
            select(  # type: ignore[call-overload]
                SummerLeagueRawFile.relative_path,
                SummerLeagueRawFile.sha256,
                SummerLeagueRawFile.parse_status,
                SummerLeagueRawFile.row_count,
            ).order_by(SummerLeagueRawFile.relative_path)
        )
    ).all()
    _add_rows(digest, label="raw_files", rows=raw_files)

    source_players = (
        await db.execute(
            select(  # type: ignore[call-overload]
                SummerLeagueSourcePlayer.nba_stats_person_id,
                SummerLeagueSourcePlayer.canonical_player_id,
                SummerLeagueSourcePlayer.resolution_status,
            ).order_by(SummerLeagueSourcePlayer.nba_stats_person_id)
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
                SummerLeagueCompetition.id,
                SummerLeagueCompetition.year,
                SummerLeagueCompetition.venue_slug,
            ).order_by(SummerLeagueCompetition.id)
        )
    ).all()
    _add_rows(digest, label="competitions", rows=competitions)
    return digest.hexdigest()
