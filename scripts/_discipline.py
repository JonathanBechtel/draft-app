"""Shared vocabulary for the ``# discipline:`` waiver convention.

Every guardrail in ``docs/plans/programmatic-code-discipline.md`` ships with an escape hatch,
because a rule with no way out gets bypassed wholesale the first time it is wrong. The repo's
homegrown AST checkers all spell that escape hatch the same way::

    # discipline: <rule> <reason>

The reason is **mandatory**. A bare marker is not a waiver — the point of the convention is
that exceptions are visible and argued in review rather than silently accumulated.

This module is the single definition of that syntax so the checkers cannot fork it (one
gaining multi-line reasons, another quietly accepting a bare marker). Third-party guardrails
are deliberately *not* forced through here: ruff has ``per-file-ignores`` and import-linter has
``ignore_imports``, and using each tool's own native, greppable ratchet beats inventing a
parallel scheme on top of it.

Consumers: ``check_unscoped_delete.py``, ``check_file_size_ratchet.py``, and the Tier-1
checkers the roadmap still has queued (stat-constant confinement, transaction body weight).
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=None)
def waiver_pattern(rule: str) -> re.Pattern[str]:
    """Return the compiled waiver pattern for ``rule``.

    Args:
        rule: The guardrail's slug, e.g. ``"unscoped-delete"`` or ``"file-size"``.

    Returns:
        A pattern whose ``reason`` group holds whatever follows the marker.
    """
    return re.compile(rf"#\s*discipline:\s*{re.escape(rule)}\b(?P<reason>.*)")


def line_has_reasoned_waiver(line: str, rule: str) -> bool:
    """Return True if ``line`` carries a waiver for ``rule`` with a non-empty reason."""
    match = waiver_pattern(rule).search(line)
    return bool(match and match.group("reason").strip())


def text_has_reasoned_waiver(text: str, rule: str) -> bool:
    """Return True if any line of ``text`` carries a justified waiver for ``rule``."""
    return any(line_has_reasoned_waiver(line, rule) for line in text.splitlines())
