"""Source-agnostic statistical engine — one home for every box-derived formula.

This package is the source-agnostic home for formulas shared by stat read paths and
materialization jobs.  Callers provide neutral values or SQL field resolvers; this package
never imports Summer League schemas or services.

**Contract** (enforced by import-linter; see ``[tool.importlinter]`` in ``pyproject.toml``):
nothing here may import ``app.services.summer_league*`` or ``app.schemas.summer_league*``. The
engine takes neutral inputs and returns numbers; Summer League is one caller, not its shape.
The contract exists before the code so the engine cannot acquire a spoke dependency on its
first day — see ``docs/plans/programmatic-code-discipline.md`` §3.1.
"""
