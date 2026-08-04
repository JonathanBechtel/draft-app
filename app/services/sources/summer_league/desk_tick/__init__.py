"""Latency-class partition of the Summer League Desk tick (#699).

`docs/plans/summer-league-desk-simplification-spec.md` §2. One orchestrator
used to run work with wildly different latency profiles behind a single shared
writer lock, so the fastest-moving surface was gated by the slowest work in the
run -- and it failed most often *while games were live*, because that is when
ingestion has the most to process. This package splits it three ways:

======================  ==========  ==============  ==================
Module                  Cadence     Budget          Writer lock
======================  ==========  ==============  ==================
:mod:`.fast`            minutes     seconds         none
:mod:`.projection`      ~hourly     < 1 min         none
:mod:`.backbone`        hours       unbounded       the shared lock
======================  ==========  ==============  ==================

:mod:`.composite` reassembles all three into the unchanged ``run_desk_tick``,
which the pre-partition cron still runs and which serves as the equivalence
oracle for the split.

The goal is **reliability, not speed**. Hourly remains the intended cadence and
is not in question (spec "Product decisions", settled). The acceptance signal
is the percentage of scheduled ticks that complete with advanced source data
measured *specifically within live-game windows* -- never daily-averaged, since
off-peak ticks succeed easily and mask exactly the misses that are the whole
user-visible problem.
"""

from app.services.sources.summer_league.desk_tick.backbone import (
    BackboneTickResult,
    run_backbone_tick,
)
from app.services.sources.summer_league.desk_tick.composite import (
    DeskTickResult,
    run_desk_tick,
)
from app.services.sources.summer_league.desk_tick.fast import (
    FastTickResult,
    WindowResolution,
    resolve_window,
    run_fast_tick,
)
from app.services.sources.summer_league.desk_tick.projection import (
    ProjectionTickResult,
    run_projection_tick,
)
from app.services.sources.summer_league.desk_tick.shared import (
    DEFAULT_RAW_ROOT,
    DEFAULT_WRITER_LOCK_MAX_WAIT_SECONDS,
    NO_WRITER_LOCK,
    DeskLatencyClass,
    TickContext,
    WriterLockPolicy,
)

__all__ = [
    "DEFAULT_RAW_ROOT",
    "DEFAULT_WRITER_LOCK_MAX_WAIT_SECONDS",
    "NO_WRITER_LOCK",
    "BackboneTickResult",
    "DeskLatencyClass",
    "DeskTickResult",
    "FastTickResult",
    "ProjectionTickResult",
    "TickContext",
    "WindowResolution",
    "WriterLockPolicy",
    "resolve_window",
    "run_backbone_tick",
    "run_desk_tick",
    "run_fast_tick",
    "run_projection_tick",
]
