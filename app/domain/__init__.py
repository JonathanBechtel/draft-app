"""Shared domain vocabulary — plain value objects, free of ORM and framework types.

This package is the code-level home of the journey-graph vocabulary described in
``docs/plans/journey-graph-domain-vocabulary.md``: ``Watermark``, ``VersionStamps``,
``Scope``, and the identity/spoke types that follow them.

It is deliberately empty today. Phase 4 of ``docs/plans/summer-league-remediation-roadmap.md``
populates it, starting with ``temporal.py``.

**Contract** (enforced by import-linter; see ``[tool.importlinter]`` in ``pyproject.toml``):
nothing here may import ``app.schemas``, ``app.services``, or ``app.routes``. The vocabulary
describes the domain; it does not know how the domain is persisted or served. Writing that
contract before the package exists is why it starts green and stays green — see
``docs/plans/programmatic-code-discipline.md`` §3.1.
"""

from app.domain.temporal import Scope, VersionStamps, Watermark

__all__ = ["Scope", "VersionStamps", "Watermark"]
