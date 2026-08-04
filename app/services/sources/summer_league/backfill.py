"""Orchestrate Summer League raw audit, normalization, and player resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.sources.summer_league.audit import (
    SummerLeagueAuditReport,
    audit_summer_league_raw,
)
from app.services.sources.summer_league.normalization import (
    SummerLeagueNormalizationReport,
    SummerLeaguePlayerLogReport,
    normalize_competition_games,
    normalize_player_game_logs,
)
from app.services.sources.summer_league.player_resolution import (
    SummerLeagueResolutionReport,
    SummerLeagueResolutionResult,
    resolve_summer_league_players,
)


@dataclass(frozen=True, slots=True)
class SummerLeagueBackfillOptions:
    """User-selected Summer League backfill scope and behavior.

    Attributes:
        include_resolution: Whether to run
            :func:`~app.services.sources.summer_league.player_resolution.resolve_summer_league_players`
            as part of this call. Callers that must not hold a database
            transaction/writer lock across candidate search's Gemini call
            (e.g. the scheduled ingest cron) should pass ``False`` here and
            run resolution separately via
            :func:`~app.services.sources.summer_league.player_resolution.prepare_summer_league_player_resolutions`
            and
            :func:`~app.services.sources.summer_league.player_resolution.apply_source_player_resolution_plan`
            in their own bounded batches.
    """

    year: int
    league_id: str
    raw_root: Path
    dry_run: bool = False
    force: bool = False
    limit_games: int | None = None
    create_stubs: bool = False
    include_resolution: bool = True


@dataclass(frozen=True, slots=True)
class SummerLeagueBackfillReport:
    """Structured report from one Summer League backfill workflow."""

    year: int
    league_id: str
    raw_root: Path
    dry_run: bool
    force: bool
    limit_games: int | None
    create_stubs: bool
    audit: SummerLeagueAuditReport
    competition_games: SummerLeagueNormalizationReport | None
    player_logs: SummerLeaguePlayerLogReport | None
    resolution: SummerLeagueResolutionReport | None
    unsupported_dry_run_stages: tuple[str, ...] = ()
    stopped_after_stage: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable report payload."""
        return {
            "year": self.year,
            "league_id": self.league_id,
            "raw_root": self.raw_root.as_posix(),
            "dry_run": self.dry_run,
            "force": self.force,
            "limit_games": self.limit_games,
            "create_stubs": self.create_stubs,
            "unsupported_dry_run_stages": list(self.unsupported_dry_run_stages),
            "stopped_after_stage": self.stopped_after_stage,
            "warnings": list(self.warnings),
            "audit": _audit_counts(self.audit),
            "competition_game_team": (
                None
                if self.competition_games is None
                else _competition_counts(self.competition_games)
            ),
            "player_logs": (
                None
                if self.player_logs is None
                else _player_log_counts(self.player_logs)
            ),
            "resolution": (
                None if self.resolution is None else _resolution_counts(self.resolution)
            ),
        }

    def to_json(self) -> str:
        """Return stable pretty JSON for report files."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


class _DryRunComplete(Exception):
    """Internal signal used to roll back a dry-run savepoint."""

    def __init__(self, report: SummerLeagueBackfillReport) -> None:
        super().__init__("Summer League dry run complete")
        self.report = report


async def backfill_summer_league_backbone(
    db: AsyncSession,
    options: SummerLeagueBackfillOptions,
) -> SummerLeagueBackfillReport:
    """Run the implemented Summer League backfill stages in dependency order.

    The caller owns commit scope for normal runs. Dry runs execute inside a
    nested transaction and roll it back after building the report, allowing
    later stages to read earlier stage writes without persisting them.
    """
    if options.limit_games is not None and options.limit_games < 0:
        raise ValueError("limit_games must be non-negative")

    if options.dry_run:
        try:
            async with db.begin_nested():
                raise _DryRunComplete(await _run_backfill_stages(db, options))
        except _DryRunComplete as exc:
            return exc.report
    return await _run_backfill_stages(db, options)


def write_backfill_report(
    report: SummerLeagueBackfillReport,
    report_path: Path,
) -> None:
    """Write a Summer League backfill report JSON file."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.to_json())


def summarize_backfill_report(report: SummerLeagueBackfillReport) -> str:
    """Return a compact human-readable summary for CLI output."""
    pieces = [
        f"{report.year}/{report.league_id}",
        f"dry_run={report.dry_run}",
        f"audit_runs={report.audit.runs_scanned}",
        f"audit_files={report.audit.files_audited}",
        f"parse_failures={report.audit.parse_failures}",
    ]
    if report.competition_games is not None:
        pieces.extend(
            [
                f"teams={report.competition_games.teams_upserted}",
                f"games={report.competition_games.games_upserted}",
                f"team_logs={report.competition_games.team_game_logs_upserted}",
            ]
        )
    if report.player_logs is not None:
        pieces.extend(
            [
                f"source_players={report.player_logs.source_players_upserted}",
                f"player_logs={report.player_logs.player_game_logs_upserted}",
                f"skipped_player_logs={report.player_logs.player_game_logs_skipped}",
            ]
        )
    if report.resolution is not None:
        pieces.extend(
            [
                f"resolved={report.resolution.resolved_source_players}",
                f"unresolved={report.resolution.unresolved_source_players}",
                f"stubs={report.resolution.stubs_created}",
                f"logs_backfilled={report.resolution.player_game_logs_backfilled}",
            ]
        )
    if report.stopped_after_stage is not None:
        pieces.append(f"stopped_after={report.stopped_after_stage}")
    if report.unsupported_dry_run_stages:
        stages = ",".join(report.unsupported_dry_run_stages)
        pieces.append(f"unsupported_dry_run_stages={stages}")
    return " ".join(pieces)


async def _run_backfill_stages(
    db: AsyncSession,
    options: SummerLeagueBackfillOptions,
) -> SummerLeagueBackfillReport:
    audit_report = await audit_summer_league_raw(
        db,
        raw_root=options.raw_root,
        year=options.year,
        league_id=options.league_id,
        limit_games=options.limit_games,
    )
    warnings: list[str] = []
    if audit_report.runs_scanned == 0:
        raise ValueError(
            "No Summer League raw manifests found for "
            f"{options.year}/{options.league_id} under {options.raw_root}.\n"
            "\n"
            "Backfill reads manifests that the raw fetch writes to disk; it cannot "
            "produce them itself. Run the pipeline in order:\n"
            "\n"
            "  1. python scripts/fetch_summer_league_raw.py "
            f"--year {options.year} --league-id {options.league_id}\n"
            "  2. python scripts/audit_summer_league_raw.py "
            f"--year {options.year} --league-id {options.league_id}\n"
            "  3. this backfill\n"
            "  4. python scripts/normalize_summer_league.py\n"
            "  5. python scripts/rebuild_sl_metrics.py\n"
            "\n"
            f"If the fetch already ran, check --raw-root: expected manifests under "
            f"{options.raw_root} for season {options.year}, league {options.league_id}."
        )
    if audit_report.parse_failures and not options.force:
        warnings.append(
            "Audit found parse failures; rerun with --force to continue "
            "normalization and resolution."
        )
        return SummerLeagueBackfillReport(
            year=options.year,
            league_id=options.league_id,
            raw_root=options.raw_root,
            dry_run=options.dry_run,
            force=options.force,
            limit_games=options.limit_games,
            create_stubs=options.create_stubs,
            audit=audit_report,
            competition_games=None,
            player_logs=None,
            resolution=None,
            stopped_after_stage="audit",
            warnings=tuple(warnings),
        )

    competition_report = await normalize_competition_games(
        db,
        year=options.year,
        league_id=options.league_id,
        raw_root=options.raw_root,
        limit_games=options.limit_games,
    )
    player_log_report = await normalize_player_game_logs(
        db,
        year=options.year,
        league_id=options.league_id,
        raw_root=options.raw_root,
        limit_games=options.limit_games,
    )
    resolution_report = (
        await resolve_summer_league_players(
            db,
            year=options.year,
            league_id=options.league_id,
            create_stubs=options.create_stubs,
        )
        if options.include_resolution
        else None
    )
    return SummerLeagueBackfillReport(
        year=options.year,
        league_id=options.league_id,
        raw_root=options.raw_root,
        dry_run=options.dry_run,
        force=options.force,
        limit_games=options.limit_games,
        create_stubs=options.create_stubs,
        audit=audit_report,
        competition_games=competition_report,
        player_logs=player_log_report,
        resolution=resolution_report,
        unsupported_dry_run_stages=(),
        warnings=tuple(warnings),
    )


def _audit_counts(report: SummerLeagueAuditReport) -> dict[str, Any]:
    return {
        "runs_scanned": report.runs_scanned,
        "files_audited": report.files_audited,
        "parse_failures": report.parse_failures,
        "endpoint_coverage": report.endpoint_coverage,
        "row_counts": report.row_counts,
    }


def _competition_counts(report: SummerLeagueNormalizationReport) -> dict[str, Any]:
    return {
        "year": report.year,
        "league_id": report.league_id,
        "competition_id": report.competition_id,
        "teams_upserted": report.teams_upserted,
        "games_upserted": report.games_upserted,
        "team_game_logs_upserted": report.team_game_logs_upserted,
        "data_quality": report.data_quality.value,
    }


def _player_log_counts(report: SummerLeaguePlayerLogReport) -> dict[str, Any]:
    return {
        "year": report.year,
        "league_id": report.league_id,
        "competition_id": report.competition_id,
        "source_players_upserted": report.source_players_upserted,
        "player_game_logs_upserted": report.player_game_logs_upserted,
        "player_game_logs_skipped": report.player_game_logs_skipped,
    }


def _resolution_counts(report: SummerLeagueResolutionReport) -> dict[str, Any]:
    return {
        "year": report.year,
        "league_id": report.league_id,
        "total_source_players": report.total_source_players,
        "resolved_source_players": report.resolved_source_players,
        "unresolved_source_players": report.unresolved_source_players,
        "external_id_resolutions": report.external_id_resolutions,
        "existing_source_resolutions": report.existing_source_resolutions,
        "exact_resolutions": report.exact_resolutions,
        "alias_resolutions": report.alias_resolutions,
        "candidate_source_players": report.candidate_source_players,
        "stubs_created": report.stubs_created,
        "player_game_logs_backfilled": report.player_game_logs_backfilled,
        "results": [_resolution_result_payload(result) for result in report.results],
    }


def _resolution_result_payload(
    result: SummerLeagueResolutionResult,
) -> dict[str, Any]:
    return {
        "source_player_id": result.source_player_id,
        "nba_stats_person_id": result.nba_stats_person_id,
        "raw_player_name": result.raw_player_name,
        "player_id": result.player_id,
        "status": result.status.value,
        "method": result.method,
        "confidence": result.confidence,
        "external_id_created": result.external_id_created,
        "stub_created": result.stub_created,
        "logs_backfilled": result.logs_backfilled,
        "candidates": [
            {
                "player_id": candidate.player_id,
                "display_name": candidate.display_name,
                "score": candidate.score,
                "method": candidate.method,
            }
            for candidate in result.candidates
        ],
    }
