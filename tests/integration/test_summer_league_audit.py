"""Integration tests for Summer League raw audit upserts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.summer_league import (
    SummerLeagueSourceDocument,
    SummerLeagueRawFileStatus,
    SummerLeagueIngestionRun,
    SummerLeagueRawRunStatus,
)
from app.services.sources.summer_league.audit import audit_summer_league_raw


def _payload(row_count: int, *, game_ids: list[str] | None = None) -> dict[str, object]:
    headers = ["GAME_ID"] if game_ids is not None else ["PLAYER_ID"]
    rows: list[list[object]] = (
        [[game_id] for game_id in game_ids]
        if game_ids is not None
        else [[index] for index in range(row_count)]
    )
    return {"resultSets": [{"name": "Result", "headers": headers, "rowSet": rows}]}


def _write_fixture_run(raw_root: Path) -> None:
    run_dir = raw_root / "2024" / "15"
    game_dir = run_dir / "games" / "1522400001"
    game_dir.mkdir(parents=True)
    manifest = {
        "year": 2024,
        "league_id": "15",
        "venue": "las_vegas",
        "started_at": "2026-06-07T12:00:00Z",
        "finished_at": "2026-06-07T12:04:00Z",
        "team_gamelog_rows": 1,
        "player_gamelog_rows": 2,
        "game_ids": ["1522400001"],
        "game_count": 1,
        "files_written": [],
        "files_skipped": [],
        "errors": [],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    (run_dir / "leaguegamelog_team.json").write_text(
        json.dumps(_payload(1, game_ids=["1522400001"]))
    )
    (run_dir / "leaguegamelog_player.json").write_text(json.dumps(_payload(2)))
    (game_dir / "boxscoretraditionalv2.json").write_text(json.dumps(_payload(3)))
    (game_dir / "boxscoreadvancedv2.json").write_text(json.dumps(_payload(1)))
    (game_dir / "boxscorescoringv2.json").write_text(json.dumps(_payload(1)))
    (game_dir / "playbyplayv2.json").write_text(json.dumps(_payload(4)))
    (game_dir / "shotchartdetail.json").write_text(json.dumps(_payload(5)))


@pytest.mark.asyncio
async def test_audit_summer_league_raw_upserts_idempotently(
    db_session: AsyncSession,
    tmp_path: Path,
) -> None:
    """Running the audit twice keeps stable raw run and file row counts."""
    _write_fixture_run(tmp_path)

    first = await audit_summer_league_raw(
        db_session,
        raw_root=tmp_path,
        year=2024,
        league_id="15",
        s3_prefix="s3://bucket/raw/nba_stats/summer_league",
    )
    second = await audit_summer_league_raw(
        db_session,
        raw_root=tmp_path,
        year=2024,
        league_id="15",
        s3_prefix="s3://bucket/raw/nba_stats/summer_league",
    )

    raw_run_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeagueIngestionRun)
    )
    raw_file_count = await db_session.scalar(
        select(func.count()).select_from(SummerLeagueSourceDocument)
    )
    raw_run = (await db_session.execute(select(SummerLeagueIngestionRun))).scalar_one()
    shotchart = (
        await db_session.execute(
            select(SummerLeagueSourceDocument).where(
                SummerLeagueSourceDocument.endpoint == "shotchartdetail"
            )
        )
    ).scalar_one()

    assert first.files_audited == 8
    assert second.files_audited == 8
    assert raw_run_count == 1
    assert raw_file_count == 8
    assert raw_run.status == SummerLeagueRawRunStatus.COMPLETE
    assert raw_run.manifest_sha256 is not None
    assert shotchart.parse_status == SummerLeagueRawFileStatus.PARSED
    assert shotchart.row_count == 5
    assert shotchart.s3_key == (
        "raw/nba_stats/summer_league/2024/15/games/1522400001/shotchartdetail.json"
    )
