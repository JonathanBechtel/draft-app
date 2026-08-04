"""Unit tests for the Summer League backbone backfill coordinator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.schemas.summer_league import (
    SummerLeagueDataQuality,
    SummerLeagueRawFileStatus,
    SummerLeagueRawRunStatus,
)
from app.services.sources.summer_league import backfill
from app.services.sources.summer_league.audit import (
    AuditedRawFile,
    AuditedRawRun,
    RawFileDescriptor,
    SummerLeagueAuditReport,
)
from app.services.sources.summer_league.normalization import (
    SummerLeagueNormalizationReport,
    SummerLeaguePlayerLogReport,
)
from app.services.backbone.player_resolution import (
    SummerLeagueResolutionReport,
)


class FakeTransaction:
    """Minimal async transaction for dry-run coordinator tests."""

    def __init__(self) -> None:
        self.rolled_back = False

    async def __aenter__(self) -> "FakeTransaction":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        if exc_type is not None:
            self.rolled_back = True


class FakeSession:
    """Minimal AsyncSession stand-in exposing begin_nested."""

    def __init__(self) -> None:
        self.transaction = FakeTransaction()

    def begin_nested(self) -> FakeTransaction:
        """Return a reusable fake nested transaction."""
        return self.transaction


def _audit_report(*, parse_failure: bool = False) -> SummerLeagueAuditReport:
    file = AuditedRawFile(
        descriptor=RawFileDescriptor(
            relative_path="2024/15/leaguegamelog_team.json",
            endpoint="leaguegamelog_team",
        ),
        sha256="abc",
        byte_size=10,
        row_count=None,
        parse_status=(
            SummerLeagueRawFileStatus.PARSE_FAILED
            if parse_failure
            else SummerLeagueRawFileStatus.PARSED
        ),
    )
    run = AuditedRawRun(
        year=2024,
        league_id="15",
        venue_slug="las_vegas",
        status=(
            SummerLeagueRawRunStatus.FAILED
            if parse_failure
            else SummerLeagueRawRunStatus.COMPLETE
        ),
        manifest_path="2024/15/manifest.json",
        manifest_sha256="manifest",
        s3_manifest_key=None,
        started_at=None,
        finished_at=None,
        team_gamelog_rows=2,
        player_gamelog_rows=1,
        game_count=1,
        error_count=0,
        files=(file,),
    )
    return SummerLeagueAuditReport(raw_root=Path("raw"), runs=(run,))


@pytest.mark.asyncio
async def test_backfill_runs_stages_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coordinator calls each stage with the selected slice options."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_audit(_db: object, **kwargs: Any) -> SummerLeagueAuditReport:
        calls.append(("audit", kwargs))
        return _audit_report()

    async def fake_competition(
        _db: object, **kwargs: Any
    ) -> SummerLeagueNormalizationReport:
        calls.append(("competition", kwargs))
        return SummerLeagueNormalizationReport(
            year=2024,
            league_id="15",
            competition_id=11,
            teams_upserted=2,
            games_upserted=1,
            team_game_logs_upserted=2,
            data_quality=SummerLeagueDataQuality.FULL,
        )

    async def fake_player_logs(
        _db: object, **kwargs: Any
    ) -> SummerLeaguePlayerLogReport:
        calls.append(("player_logs", kwargs))
        return SummerLeaguePlayerLogReport(
            year=2024,
            league_id="15",
            competition_id=11,
            source_players_upserted=1,
            player_game_logs_upserted=1,
            player_game_logs_skipped=0,
        )

    async def fake_resolution(
        _db: object, **kwargs: Any
    ) -> SummerLeagueResolutionReport:
        calls.append(("resolution", kwargs))
        return SummerLeagueResolutionReport(
            year=2024,
            league_id="15",
            total_source_players=1,
            resolved_source_players=1,
            unresolved_source_players=0,
            external_id_resolutions=0,
            existing_source_resolutions=0,
            exact_resolutions=1,
            alias_resolutions=0,
            candidate_source_players=0,
            stubs_created=0,
            player_game_logs_backfilled=1,
            participation_rows_backfilled=0,
        )

    monkeypatch.setattr(backfill, "audit_summer_league_raw", fake_audit)
    monkeypatch.setattr(backfill, "normalize_competition_games", fake_competition)
    monkeypatch.setattr(backfill, "normalize_player_game_logs", fake_player_logs)
    monkeypatch.setattr(backfill, "resolve_summer_league_players", fake_resolution)

    report = await backfill.backfill_summer_league_backbone(
        FakeSession(),  # type: ignore[arg-type]
        backfill.SummerLeagueBackfillOptions(
            year=2024,
            league_id="15",
            raw_root=Path("raw"),
            limit_games=1,
            create_stubs=True,
        ),
    )

    assert [name for name, _kwargs in calls] == [
        "audit",
        "competition",
        "player_logs",
        "resolution",
    ]
    assert calls[0][1]["limit_games"] == 1
    assert calls[1][1]["limit_games"] == 1
    assert calls[2][1]["limit_games"] == 1
    assert calls[3][1]["create_stubs"] is True
    assert report.to_dict()["competition_game_team"]["games_upserted"] == 1
    assert report.unsupported_dry_run_stages == ()


@pytest.mark.asyncio
async def test_dry_run_rolls_back_nested_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry runs execute the pipeline but roll back the nested transaction."""

    async def fake_audit(_db: object, **_kwargs: Any) -> SummerLeagueAuditReport:
        return _audit_report()

    async def fake_competition(
        _db: object, **_kwargs: Any
    ) -> SummerLeagueNormalizationReport:
        return SummerLeagueNormalizationReport(
            year=2024,
            league_id="15",
            competition_id=11,
            teams_upserted=0,
            games_upserted=0,
            team_game_logs_upserted=0,
            data_quality=SummerLeagueDataQuality.RAW_ONLY,
        )

    async def fake_player_logs(
        _db: object, **_kwargs: Any
    ) -> SummerLeaguePlayerLogReport:
        return SummerLeaguePlayerLogReport(
            year=2024,
            league_id="15",
            competition_id=11,
            source_players_upserted=0,
            player_game_logs_upserted=0,
            player_game_logs_skipped=0,
        )

    async def fake_resolution(
        _db: object, **_kwargs: Any
    ) -> SummerLeagueResolutionReport:
        return SummerLeagueResolutionReport(
            year=2024,
            league_id="15",
            total_source_players=0,
            resolved_source_players=0,
            unresolved_source_players=0,
            external_id_resolutions=0,
            existing_source_resolutions=0,
            exact_resolutions=0,
            alias_resolutions=0,
            candidate_source_players=0,
            stubs_created=0,
            player_game_logs_backfilled=0,
            participation_rows_backfilled=0,
        )

    monkeypatch.setattr(backfill, "audit_summer_league_raw", fake_audit)
    monkeypatch.setattr(backfill, "normalize_competition_games", fake_competition)
    monkeypatch.setattr(backfill, "normalize_player_game_logs", fake_player_logs)
    monkeypatch.setattr(backfill, "resolve_summer_league_players", fake_resolution)
    session = FakeSession()

    report = await backfill.backfill_summer_league_backbone(
        session,  # type: ignore[arg-type]
        backfill.SummerLeagueBackfillOptions(
            year=2024,
            league_id="15",
            raw_root=Path("raw"),
            dry_run=True,
        ),
    )

    assert session.transaction.rolled_back is True
    assert report.dry_run is True
    assert report.unsupported_dry_run_stages == ()


@pytest.mark.asyncio
async def test_backfill_stops_after_audit_failures_without_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit parse failures stop later stages unless force is enabled."""
    called_competition = False

    async def fake_audit(_db: object, **_kwargs: Any) -> SummerLeagueAuditReport:
        return _audit_report(parse_failure=True)

    async def fake_competition(_db: object, **_kwargs: Any) -> object:
        nonlocal called_competition
        called_competition = True
        return object()

    monkeypatch.setattr(backfill, "audit_summer_league_raw", fake_audit)
    monkeypatch.setattr(backfill, "normalize_competition_games", fake_competition)

    report = await backfill.backfill_summer_league_backbone(
        FakeSession(),  # type: ignore[arg-type]
        backfill.SummerLeagueBackfillOptions(
            year=2024,
            league_id="15",
            raw_root=Path("raw"),
        ),
    )

    assert report.stopped_after_stage == "audit"
    assert report.player_logs is None
    assert called_competition is False
    assert report.warnings


@pytest.mark.asyncio
async def test_missing_raw_manifests_error_names_the_pipeline_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping the raw fetch must fail with actionable ordering guidance.

    Backfill is stage 3 of a five-stage pipeline and reads manifests the raw fetch wrote to
    disk; it cannot produce them itself. The error previously stated only that manifests
    were missing, which does not tell an operator whether they skipped stage 1 or simply
    pointed --raw-root at the wrong directory. Both remedies must appear.
    """

    async def fake_audit(_db: object, **_kwargs: Any) -> SummerLeagueAuditReport:
        return SummerLeagueAuditReport(raw_root=Path("raw"), runs=())

    monkeypatch.setattr(backfill, "audit_summer_league_raw", fake_audit)

    with pytest.raises(ValueError) as excinfo:
        await backfill.backfill_summer_league_backbone(
            FakeSession(),  # type: ignore[arg-type]
            backfill.SummerLeagueBackfillOptions(
                year=2024,
                league_id="15",
                raw_root=Path("raw"),
            ),
        )

    message = str(excinfo.value)
    assert "No Summer League raw manifests found" in message
    # The ordered pipeline, so a skipped stage 1 is obvious.
    for stage in (
        "fetch_summer_league_raw.py",
        "audit_summer_league_raw.py",
        "normalize_summer_league.py",
        "rebuild_sl_metrics.py",
    ):
        assert stage in message, f"missing {stage} from the ordering guidance"
    # The scope actually searched, so a wrong path is not misdiagnosed as a missing fetch.
    assert "--raw-root" in message
    assert "2024" in message and "15" in message
