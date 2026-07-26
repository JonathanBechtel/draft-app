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
"""
