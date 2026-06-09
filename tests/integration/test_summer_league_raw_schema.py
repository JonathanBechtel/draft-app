"""Integration tests for Summer League raw audit schema contracts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueRawFile,
    SummerLeagueRawFileStatus,
    SummerLeagueRawRun,
    SummerLeagueRawRunStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _raw_run(
    *,
    manifest_path: str = "2024/15/manifest.json",
    league_id: str = "15",
) -> SummerLeagueRawRun:
    return SummerLeagueRawRun(
        year=2024,
        league_id=league_id,
        venue_slug="las_vegas",
        status=SummerLeagueRawRunStatus.COMPLETE,
        started_at=_now(),
        finished_at=_now(),
        team_gamelog_rows=12,
        player_gamelog_rows=120,
        game_count=1,
        error_count=0,
        manifest_path=manifest_path,
        manifest_sha256="a" * 64,
        s3_manifest_key=f"raw/nba_stats/summer_league/{manifest_path}",
    )


def _raw_file(
    raw_run_id: int,
    *,
    endpoint: str = "boxscoretraditionalv2",
    game_id: str | None = "1522400001",
    relative_path: str = "2024/15/games/1522400001/boxscoretraditionalv2.json",
) -> SummerLeagueRawFile:
    return SummerLeagueRawFile(
        raw_run_id=raw_run_id,
        year=2024,
        league_id="15",
        endpoint=endpoint,
        game_id=game_id,
        relative_path=relative_path,
        s3_key=f"raw/nba_stats/summer_league/{relative_path}",
        sha256="b" * 64,
        byte_size=512,
        row_count=14,
        parse_status=SummerLeagueRawFileStatus.PARSED,
        audited_at=_now(),
    )


@pytest.mark.asyncio
async def test_raw_audit_rows_persist_enum_values_and_metadata(
    db_session: AsyncSession,
) -> None:
    """Raw run/file rows persist enum values and archive metadata."""
    raw_run = _raw_run()
    db_session.add(raw_run)
    await db_session.flush()

    assert raw_run.id is not None
    raw_file = _raw_file(raw_run.id)
    db_session.add(raw_file)
    await db_session.flush()
    await db_session.refresh(raw_file)

    assert raw_run.status == SummerLeagueRawRunStatus.COMPLETE
    assert raw_run.manifest_sha256 == "a" * 64
    assert (
        raw_run.s3_manifest_key == "raw/nba_stats/summer_league/2024/15/manifest.json"
    )
    assert raw_file.parse_status == SummerLeagueRawFileStatus.PARSED
    assert raw_file.byte_size == 512
    assert raw_file.s3_key == (
        "raw/nba_stats/summer_league/2024/15/games/1522400001/"
        "boxscoretraditionalv2.json"
    )


@pytest.mark.asyncio
async def test_raw_run_unique_year_league_manifest_path(
    db_session: AsyncSession,
) -> None:
    """One raw run is allowed per year, LeagueID, and manifest path."""
    db_session.add(_raw_run())
    await db_session.flush()
    db_session.add(_raw_run())

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_raw_file_unique_relative_path(db_session: AsyncSession) -> None:
    """A raw file relative path can be audited only once globally."""
    raw_run = _raw_run()
    db_session.add(raw_run)
    await db_session.flush()
    assert raw_run.id is not None

    db_session.add(_raw_file(raw_run.id))
    await db_session.flush()
    db_session.add(
        _raw_file(
            raw_run.id,
            endpoint="boxscoreadvancedv2",
            relative_path="2024/15/games/1522400001/boxscoretraditionalv2.json",
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_raw_file_unique_run_endpoint_game(db_session: AsyncSession) -> None:
    """A raw run can have only one audited row per endpoint and game ID."""
    raw_run = _raw_run()
    db_session.add(raw_run)
    await db_session.flush()
    assert raw_run.id is not None

    db_session.add(_raw_file(raw_run.id))
    await db_session.flush()
    db_session.add(
        _raw_file(
            raw_run.id,
            relative_path="2024/15/games/1522400001/boxscoretraditionalv2-copy.json",
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
