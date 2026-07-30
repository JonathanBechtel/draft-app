"""Source-agnostic statistical engine — one home for every box-derived formula.

This package is where the shared stat engine lands: the single implementation of TS%, eFG%,
TOV%, Game Score, per-36/per-100 scaling, and the percentile/baseline normalizer that are
today implemented three-to-eight times each across Python and hand-written SQL
(``docs/plans/summer-league-simplification-backlog.md`` bucket 1).

It is deliberately empty today. Phase 2 of ``docs/plans/summer-league-remediation-roadmap.md``
lifts the engine here behind a golden-number parity harness.

**Contract** (enforced by import-linter; see ``[tool.importlinter]`` in ``pyproject.toml``):
nothing here may import ``app.services.summer_league*`` or ``app.schemas.summer_league*``. The
engine takes neutral inputs and returns numbers; Summer League is one caller, not its shape.
The contract exists before the code so the engine cannot acquire a spoke dependency on its
first day — see ``docs/plans/programmatic-code-discipline.md`` §3.1.

**T2 (Phase 2, #722) populates this package** by lifting the pure engine out of
``app.services.summer_league.metrics`` — a pure move, zero behavior change, pinned by the
golden-number parity harness in ``tests/unit/test_stat_engine_parity.py`` and
``tests/integration/test_stat_engine_parity.py``:

* :mod:`app.services.stats.inputs` — the neutral input/DTO dataclasses: :class:`StatInputs`
  (formerly ``Box``), :class:`PoolContext` (formerly ``LeagueContext``), and
  :class:`PlayerSeason`.
* :mod:`app.services.stats.formulas` — the pure formula functions: :func:`game_score`,
  :func:`game_score_line`, :func:`game_score_from_row`, the line-grain rate helpers
  (:func:`ts_pct_line`, :func:`efg_pct_line`, :func:`fg3ar_line`, :func:`ftr_line`,
  :func:`tov_pct_line`, :func:`ast_pct_line`, :func:`game_advanced_line` — each with an
  unrounded ``*_ratio`` twin for callers that pool several box lines before applying
  display rounding once), the Dean Oliver rating builders (:func:`compute_uper`,
  :func:`compute_ortg`, :func:`compute_drtg`), and the season-basket assembler
  :func:`compute_metrics`.

``app.services.summer_league.metrics`` re-exports every one of these names under their
historical names (``Box``, ``LeagueContext``, ``_d``, ...) so no caller outside the engine had
to change for this move. Everything DB-bound — loading raw logs, the SL-native Pythagorean/BPM
coefficient fits, and persisting the materialized projection (``compute``, ``rebuild``,
``rebuild_staged``) — stays in ``app.services.summer_league.metrics``; the seam is *pure
function vs. orchestration*, not "everything that used to be in metrics.py".
"""

from app.services.stats.formulas import (
    ast_pct_line,
    compute_drtg,
    compute_metrics,
    compute_ortg,
    compute_uper,
    efg_pct_line,
    efg_pct_ratio,
    fg3ar_line,
    fg3ar_ratio,
    game_advanced_line,
    game_score,
    game_score_from_row,
    game_score_line,
    ftr_line,
    ftr_ratio,
    ts_pct_line,
    ts_pct_ratio,
    tov_pct_line,
    tov_pct_ratio,
)
from app.services.stats.inputs import PlayerSeason, PoolContext, StatInputs

__all__ = [
    "PlayerSeason",
    "PoolContext",
    "StatInputs",
    "ast_pct_line",
    "compute_drtg",
    "compute_metrics",
    "compute_ortg",
    "compute_uper",
    "efg_pct_line",
    "efg_pct_ratio",
    "fg3ar_line",
    "fg3ar_ratio",
    "ftr_line",
    "ftr_ratio",
    "game_advanced_line",
    "game_score",
    "game_score_from_row",
    "game_score_line",
    "ts_pct_line",
    "ts_pct_ratio",
    "tov_pct_line",
    "tov_pct_ratio",
]
