"""Unit tests for the Desk latency-class CLI entrypoints (#699).

The four entrypoints are thin by design -- argument parsing, a summary line,
and the mapping from a run's result to the ``summer_league_pipeline_states``
columns it is entitled to stamp. That last part is the one with teeth, and it
is what most of this file tests.

**Why the completion flags matter.** Each class writes its own pipeline row,
and the whole point of per-class rows is that a healthy class cannot mask a
broken one. If the fast poller stamped ``projections_refreshed`` it would look
like the Desk was rebuilding content while the projection class had been dead
for hours -- the "rendered attractively instead of clearly stating that its
data was stale" failure the spec's §4 names. These tests pin each class to
claiming only what it actually did.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import app.cli.sl_desk_backbone_tick as backbone_cli
import app.cli.sl_desk_fast_tick as fast_cli
import app.cli.sl_desk_projection_tick as projection_cli
import app.cli.sl_desk_tick as composite_cli
from app.cli._desk_class_runner import CompletionFlags, parse_now
from app.schemas.event_desk import EventDailyState
from app.services.summer_league.desk_tick.backbone import BackboneTickResult
from app.services.summer_league.desk_tick.composite import DeskTickResult
from app.services.summer_league.desk_tick.fast import FastTickResult
from app.services.summer_league.desk_tick.projection import ProjectionTickResult
from app.services.summer_league.desk_tick.shared import DeskLatencyClass
from app.services.summer_league.live_ingestion import LiveIngestionReport
from app.services.summer_league.scoreboard_ingest import ScoreboardIngestReport

_NOW = datetime(2026, 7, 10, 19, 30)


def _fast_result(*, dormant: bool = False, **overrides: object) -> FastTickResult:
    scoreboard = ScoreboardIngestReport()
    scoreboard.competitions_checked = 1
    scoreboard.games_updated = 3
    defaults: dict[str, object] = {
        "now": _NOW,
        "executed_at": _NOW,
        "dormant": dormant,
        "daily_state": None if dormant else EventDailyState.LIVE,
        "scoreboard_report": None if dormant else scoreboard,
        "live_refresh_report": None if dormant else LiveIngestionReport(selected=6),
    }
    defaults.update(overrides)
    return FastTickResult(**defaults)  # type: ignore[arg-type]


def _projection_result(*, dormant: bool = False) -> ProjectionTickResult:
    return ProjectionTickResult(
        now=_NOW,
        executed_at=_NOW,
        dormant=dormant,
        daily_state=None if dormant else EventDailyState.LIVE,
        baseline_version=None if dormant else "v1",
        materialized_variant_count=0 if dormant else 72,
    )


def _backbone_result(*, dormant: bool = False) -> BackboneTickResult:
    return BackboneTickResult(
        now=_NOW,
        executed_at=_NOW,
        dormant=dormant,
        daily_state=None if dormant else EventDailyState.LIVE,
        normalized_competition_ids=() if dormant else (7,),
        metrics_rebuilt=not dormant,
    )


class TestCompletionFlags:
    """Each class may stamp only the freshness columns it earned."""

    def test_fast_class_claims_source_but_never_projection(self) -> None:
        """A healthy poller must not make a dead projection class look alive."""
        flags = fast_cli._completion_flags(_fast_result())
        assert flags.source_refreshed is True
        assert flags.source_advanced is True
        assert flags.projections_refreshed is False
        assert flags.snapshots_materialized is False
        assert flags.metrics_rebuilt is False
        assert flags.content_updated is False

    def test_projection_class_claims_content_but_never_source(self) -> None:
        """It talks to no provider, so it cannot vouch for basketball freshness.

        Stamping ``source_refreshed`` here would make a projection rebuilt from
        hours-old inputs look like fresh source data -- the exact inversion the
        freshness contract (spec §1) forbids.
        """
        flags = projection_cli._completion_flags(_projection_result())
        assert flags.projections_refreshed is True
        assert flags.snapshots_materialized is True
        assert flags.content_updated is True
        assert flags.source_refreshed is False
        assert flags.source_advanced is False

    def test_backbone_class_claims_metrics_but_never_content(self) -> None:
        """It writes no Desk projection, so it must not claim content freshness."""
        flags = backbone_cli._completion_flags(_backbone_result())
        assert flags.source_advanced is True
        assert flags.metrics_rebuilt is True
        assert flags.projections_refreshed is False
        assert flags.snapshots_materialized is False
        assert flags.content_updated is False

    def test_composite_claims_everything_because_it_does_everything(self) -> None:
        """The composite is all three classes in one run, so it earns every column."""
        result = DeskTickResult(
            now=_NOW,
            executed_at=_NOW,
            dormant=False,
            daily_state=EventDailyState.LIVE,
            content_updated=True,
            source_refreshed=True,
            source_advanced=True,
            normalized_competition_ids=(7,),
            materialized_variant_count=72,
        )
        flags = composite_cli._completion_flags(result)
        assert all(
            (
                flags.source_refreshed,
                flags.source_advanced,
                flags.projections_refreshed,
                flags.metrics_rebuilt,
                flags.snapshots_materialized,
                flags.content_updated,
            )
        )

    def test_a_dormant_run_claims_nothing(self) -> None:
        """Off-window is a no-op; nothing may advance a freshness watermark."""
        assert fast_cli._completion_flags(_fast_result(dormant=True)) == CompletionFlags(
            source_refreshed=False, source_advanced=False
        )
        projection_flags = projection_cli._completion_flags(
            _projection_result(dormant=True)
        )
        assert projection_flags.content_updated is False
        assert projection_flags.snapshots_materialized is False
        backbone_flags = backbone_cli._completion_flags(_backbone_result(dormant=True))
        assert backbone_flags.source_advanced is False
        assert backbone_flags.metrics_rebuilt is False


class TestSummaries:
    """Each class's cron log line names its class and its own outcome."""

    def test_each_class_labels_itself_distinctly(self) -> None:
        """An operator reading one log line must know which class produced it."""
        assert "FAST" in fast_cli._summarize(_fast_result())
        assert "PROJECTION" in projection_cli._summarize(_projection_result())
        assert "BACKBONE" in backbone_cli._summarize(_backbone_result())

    def test_dormant_summaries_say_so_plainly(self) -> None:
        """Off-window must read as a deliberate no-op, not a silent success."""
        for text in (
            fast_cli._summarize(_fast_result(dormant=True)),
            projection_cli._summarize(_projection_result(dormant=True)),
            backbone_cli._summarize(_backbone_result(dormant=True)),
        ):
            assert "dormant" in text
            assert "no-op" in text

    def test_active_summaries_report_the_class_specific_work(self) -> None:
        """The numbers an operator would page on are present."""
        fast_text = fast_cli._summarize(_fast_result())
        assert "scoreboard:" in fast_text and "live_refresh:" in fast_text
        assert "materialized_render_snapshot_variants=72" in projection_cli._summarize(
            _projection_result()
        )
        backbone_text = backbone_cli._summarize(_backbone_result())
        assert "normalized_competitions=[7]" in backbone_text
        assert "metrics_rebuilt=true" in backbone_text


class TestParsers:
    """Argument defaults, including the backbone's deliberately longer lock wait."""

    def test_fast_and_backbone_default_the_raw_root(self) -> None:
        for module in (fast_cli, backbone_cli, composite_cli):
            args = module.build_parser().parse_args([])
            assert args.raw_root == Path("data/raw/nba_stats/summer_league")
            assert args.now is None

    def test_projection_takes_no_raw_root_because_it_reads_no_raw_data(self) -> None:
        """It is a pure reader of canonical rows; a raw root would be misleading."""
        args = projection_cli.build_parser().parse_args([])
        assert not hasattr(args, "raw_root")

    def test_backbone_waits_far_longer_for_the_lock_than_the_old_desk_bound(
        self,
    ) -> None:
        """It has no latency budget, so conceding the lock quickly buys nothing.

        The 30s bound existed to stop a *user-facing* surface being starved.
        Since #699 that surface is not behind this lock at all.
        """
        args = backbone_cli.build_parser().parse_args([])
        assert args.writer_lock_max_wait_seconds == 300.0

    def test_now_override_parses_iso8601(self) -> None:
        args = fast_cli.build_parser().parse_args(["--now", "2026-07-10T19:30:00"])
        assert parse_now(args.now) == _NOW
        assert parse_now(None) is None


@pytest.mark.asyncio
async def test_fast_session_timeout_configuration_is_session_scoped() -> None:
    """The fast class bounds both lock waits and long-running SQL statements."""
    db = AsyncMock()

    await fast_cli.configure_fast_session_timeouts(db)

    statements = [str(call.args[0]) for call in db.execute.await_args_list]
    assert "lock_timeout" in statements[0]
    assert "statement_timeout" in statements[1]
    assert db.execute.await_args_list[0].args[1] == {"timeout": "3s"}
    assert db.execute.await_args_list[1].args[1] == {"timeout": "15s"}


def test_each_entrypoint_declares_its_own_latency_class() -> None:
    """The class constant is what routes telemetry and pipeline state."""
    assert DeskLatencyClass.FAST.value == "desk_fast"
    assert DeskLatencyClass.PROJECTION.value == "desk_projection"
    assert DeskLatencyClass.BACKBONE.value == "desk_backbone"
    assert DeskLatencyClass.COMPOSITE.value == "desk"
