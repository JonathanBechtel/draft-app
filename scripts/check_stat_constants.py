"""Stat-constant confinement checker (T9, #730) -- Phase 2's closing ratchet.

Failure this descends from
---------------------------
The TS% free-throw coefficient ``0.44`` was hand-copied at 28 sites across 8 modules
before Phase 2 (docs/plans/summer-league-stat-engine-reuse-spec.md); eFG%, TOV%, and
Game Score were similarly scattered. Docs #1/#2 deleted the copies and built one engine
under ``app/services/stats/``. Without a mechanical guard, "the eight copies regrow the
next time someone needs a formula in a query" -- this is that guard. See
``docs/plans/programmatic-code-discipline.md`` §1.3.

The rules
---------
1. **Bare numeric-literal confinement (AST, "R1").** ``0.44`` -- the TS%/TOV%
   free-throw coefficient -- may not appear as a Python float literal outside
   ``app/services/stats/``. Flagged on sight: the verified inventory at T9's opening
   (issue #730) found this exact value at exactly six sites outside the package, and
   every one was either prose (docstring/comment), a declared formula string, or the
   one frozen exemption -- zero incidental uses. See the "Declared formula text is not
   in scope" and "The one frozen exemption" sections below for how those are told
   apart.

   The Hollinger Game Score weights (``0.4``, ``0.7``, ``0.3`` --
   ``app.services.stats.formulas.game_score``) are *not* flagged on a bare sighting:
   unlike 0.44, they double as ordinary scale factors elsewhere in this codebase --
   test-fixture distributions in ``app/services/summer_league/environment_fixtures.py``
   and a notability score in ``app/services/summer_league/desk_facts.py``, verified by
   grep while building this checker. A single-value rule would flag both and train
   people to bypass it. Instead this checker requires *co-occurrence*: three or more
   of these weights multiplied together inside one connected arithmetic expression
   (walking through nested ``BinOp``/``UnaryOp`` parents, stopping at any other node
   kind -- a function call boundary, a dict entry, a statement break). That shape is
   unique to Game Score in this codebase today; an ordinary scale factor only ever
   uses one such weight per expression. See ``_mult_chain_root`` and
   ``_GAME_SCORE_MIN_COOCCURRENCE``.

2. **Stat-aggregate arithmetic embedded in string/f-string text (AST, "R2").** The
   same designated coefficients (``0.44``, ``0.4``, ``0.7``, ``0.3``), immediately
   adjacent to a ``*``, inside a string or f-string literal outside
   ``app/services/stats/``. This is what an AST float-literal check cannot see: SQL
   built as Python text holds the formula as *characters*, not a numeric AST node --
   exactly the shape ``_game_score_sql`` had in
   ``app.services.summer_league_explorer_service`` before this ticket (see "What this
   checker found" below). Scoped to the same designated-coefficient set as rule 1,
   not a bare ``SUM(<box field>)`` sweep -- see "Why rule 2 is not a bare SUM() sweep"
   below for why that would be scope creep, not precision.

3. **Registry formula-text reappearance (AST, "R4"), added T10 (#741).** Exact
   reappearance, as string/f-string literal text outside ``app/services/stats/``, of
   a metric's SQL form the registry already declares via a ``*_sql_text`` function --
   whether or not the formula carries a designated coefficient at all. See "Why rule
   2 is not a bare SUM() sweep" below for why this is scoped to *registered* metrics'
   exact emitted text rather than a generic aggregate-pattern sweep.

4. **Registry expression-form reappearance (AST, "R5").** An arithmetic expression
   over ORM columns outside ``app/services/stats/`` whose normalized AST shape
   matches one emitted by a registry ``*_expr`` function. The registry function is
   evaluated with a symbolic ``box`` callable, so the rule discovers future
   expression forms from the registry instead of hand-listing metric names. Row and
   aggregate wrappers such as ``ps.fga`` and ``func.sum(ps.fga)`` are normalized to
   the same boxed column; the arithmetic structure and constants are still
   compared. This catches the SQLAlchemy-expression half of a formula even when it
   is nested inside a larger filter or sort expression.

   The registry intentionally has no ``*_expr`` forms for ``fg3ar`` or ``ftr``;
   the audit found inline filter expressions for both in the Explorer, so they are
   residue for #746 rather than a reason to weaken this registry-derived rule.

Prefer AST over regex, and why
-------------------------------
A regex sweep for ``0.44`` finds six hits in this codebase and only one matters --
the noise is what tempts someone to weaken the rule later (verified reasoning from
issue #730's inventory comment). An AST walk tells the six apart for free:

* ``app/services/summer_league_environment_service.py:993`` -- a docstring. Not an
  ``ast.Constant`` float at all; ``ast.walk`` never surfaces it to rule 1, and rule 2
  explicitly excludes docstring string nodes (``_docstring_nodes``, matching
  ``scripts/check_runtime_entrypoints.py``'s ``_docstring_nodes`` precedent).
* ``:1714`` -- a comment. Comments are not part of the AST at all.
* ``:1716`` -- ``values["turnover_rate"] = safe_ratio(box.tov, box.fga + 0.44 * box.fta
  + box.tov)``. A genuine ``ast.Constant`` float inside a ``BinOp`` -- flagged by rule
  1, then suppressed by the one frozen exemption (below).
* ``app/services/summer_league_environment_registry.py:374,375,384`` --
  ``formula=``/``denominator=``/``interpretation=`` keyword values in a
  ``MetricDefinition``-style declaration. ``ast.Constant`` **str** nodes, not floats
  -- rule 1 never sees them; rule 2 explicitly excludes them (see next section).

Declared formula text is not in scope
--------------------------------------
Declaring a formula as text next to its metric -- exactly what
``app.services.summer_league_environment_registry`` and
``app.services.stats.registry`` both do -- is the pattern this phase *promotes*, not
debt to flag or allowlist. Per T9's own inventory comment: "Do not flag them, and do
not allowlist them either -- they should be structurally out of scope." Rule 2
excludes any string that is the value of a documentation keyword
(``formula``, ``denominator``, ``interpretation``, ...; see
``_DECLARATION_PROSE_KEYWORDS``) in a call -- structural exclusion, not a waiver
entry, so it carries no allowlist upkeep.

Why rule 2 is not a bare SUM() sweep
--------------------------------------
The design doc's rule 2 example text (`SUM(fga)`, `2 * (fga`) reads as license for a
generic "any SQL aggregate over a box field" sweep. Built and run against HEAD, that
sweep also matches ``efg_pct``/``fg_pct``/``fg3_pct``/``ft_pct``/``fg3ar``/``ftr``'s
raw SQL text at ``app.services.summer_league_explorer_service:2665-2740`` -- six
pre-existing formulas that were never part of T4-T8's consolidation target list (only
``ts_pct``/``tov_pct``/``astd_pct`` got registry SQL-text forms; T6's own commit
message scopes itself to "the nine 0.44 sites") and that use no designated
coefficient at all (``efg_pct``'s ``0.5`` is not one). Flagging them here would force
an unplanned, unreviewed migration of six more formulas as a side effect of adding a
checker, or a blanket allowlist that defeats the ratchet -- neither fits "the closing
ratchet" ticket. Rule 2 instead stays scoped to the coefficients rule 1 already
names, which is precise enough to catch what Phase 2 actually promised to fix without
silently expanding that promise. Recorded here as a deliberate scope boundary, not an
oversight; a bare-``SUM()`` rule is a reasonable follow-up if those six formulas are
ever consolidated on their own ticket.

3. **Registry formula-text reappearance (AST, "R4").** T10 (#741) migrated three of
   those six holdouts -- ``efg_pct``, ``fg3ar``, ``ftr`` -- leaving ``fg_pct``/
   ``fg3_pct``/``ft_pct`` permanently out of scope (#726's explicit call: plain
   shooting percentages, never part of the registry's declared formula family). A
   blanket ``SUM(<box field>)`` sweep is therefore *still* not viable even after
   T10 -- it would flag those three forever, by design, which is exactly the
   "unplanned migration or a blanket allowlist" trap the previous section declines.
   Instead of generalizing rule 2's coefficient match, this rule flags *exact*
   reappearance of a metric the registry already declares a SQL-text form for:
   every ``*_sql_text`` function in :mod:`app.services.stats.registry` is called
   with the two canonical ``box`` shapes (bare column, ``SUM(...)``-wrapped) to
   produce its row- and aggregate-grain emitted text, and any string/f-string
   literal outside ``app/services/stats/`` containing one of those exact strings
   is flagged -- regardless of whether it carries a designated coefficient at all
   (``fg3ar``/``ftr`` carry none). Because the check is *derived from the
   registry* rather than a hand-maintained pattern list, it automatically covers
   every metric added to the registry going forward, and it does not, and cannot,
   fire on ``fg_pct``/``fg3_pct``/``ft_pct`` -- their field combinations never
   match a registered ``*_sql_text`` function's output, so they need no allowlist
   entry at all. See ``_registry_formula_reappearance_violations``.

What this checker found
-------------------------
Building it surfaced a live, un-flagged duplicate: ``_game_score_sql`` in
``app.services.summer_league_explorer_service`` held the Game Score weights as a raw
f-string (the Explorer's ``ORDER BY`` sort expression) -- T6 (#727) consolidated
``ts_pct``/``tov_pct``'s SQL forms but its own commit message names this exact
function as unaddressed precedent ("the same shape ``_game_score_sql`` already uses
for Game Score"). Since "runs clean against HEAD" is this ticket's own Definition of
Done, and the ticket exists specifically to stop formulas regrowing outside the
engine, this one was folded in as part of building the checker:
``app.services.stats.registry.game_score_sql_text`` now declares it (same pattern as
``ts_pct_sql_text``), byte-identical output, bound by a parity test
(``tests/unit/services/stats/test_sql_python_parity.py``).

The one frozen exemption
--------------------------
``app/services/summer_league_environment_service.py:1716`` computes
``FGA + 0.44*FTA + TOV`` under an explicit "Frozen contract formula (§4)" comment,
deliberately independent of the engine's pooled ``tov_pct`` (T7 / #724's decision --
do not silently repoint it). T7 declared this as its own registry entry,
:data:`app.services.stats.registry.ENVIRONMENT_TURNOVER_RATE_FROZEN`
(``metric_key="environment_turnover_rate"``, ``is_frozen_exemption=True``, a non-empty
``exemption_reason`` citing the file and line).

This checker reads :func:`app.services.stats.registry.frozen_exemptions` rather than
hand-writing an allowlist entry -- duplicating the justification into a lint config
would be a new instance of exactly the duplication this phase removes. The
consequence: **the allowlist has exactly one possible entry, and it lives in the
registry, not here.**

**Vacuity check.** A registered exemption whose cited site no longer contains a
matching literal must fail the build -- otherwise the exemption list rots into
permissiveness (T7's own framing). The exemption's ``exemption_reason`` cites a
``path:line``; this checker parses that citation and requires a real rule-1 violation
within a small forward window of the cited line (``_EXEMPTION_LINE_TOLERANCE``) --
not the exact line, because the citation text in this codebase points at the
*comment* introducing the frozen formula (line 1715) while the executable literal
sits one line later (1716); an exact-line match would report a live, correct
exemption as vacuous. A window is the same tolerance the sibling checkers'
``_waived`` helpers already give comments relative to the code they justify.

Generic escape hatch
----------------------
For anything else, the standard repo-wide convention (``scripts/_discipline.py``,
which already names "stat-constant confinement" as a planned consumer): a comment
carrying a reason, anywhere on the offending line or the line(s) directly above it::

    # discipline: stat-constants <reason>

A bare marker with no reason is rejected.

Two invocation modes
-----------------------
Zero args scans the whole ``app/`` tree (``make lint.stat-constants`` / CI) -- the
shape this checker needs regardless of what changed, since the vacuity check is a
property of the whole tree, not of a diff. Explicit paths scan just those files, for
ad-hoc/local use; the frozen-exemption vacuity check still reads its cited file
directly rather than depending on what was passed, so it cannot be defeated by a
partial invocation. This mirrors ``scripts/check_runtime_entrypoints.py`` (the
ratcheted-allowlist precedent named in this ticket) rather than
``scripts/check_unscoped_delete.py``'s pure per-path shape, because this rule -- like
the entrypoint boundary -- is a standing invariant over the whole package layout, not
a diff-scoped one.

Usage::

    python scripts/check_stat_constants.py                 # whole app/ tree
    python scripts/check_stat_constants.py <path> [<path> ...]
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from _discipline import line_has_reasoned_waiver

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.stats import registry as _stats_registry  # noqa: E402
from app.services.stats.registry import frozen_exemptions  # noqa: E402

# The escape-hatch slug; syntax and the mandatory-reason rule live in _discipline.py.
# Named here (not "0.44" or "confinement") because it is expected to cover both rules.
_RULE = "stat-constants"

# The package the confinement rule protects.
_ENGINE_PACKAGE_PREFIX = "app/services/stats/"

# Rule 1a -- flagged on sight, no co-occurrence required. See module docstring.
_TS_TOV_COEFFICIENT = 0.44

# Rule 1b -- Hollinger Game Score's per-component weights
# (app.services.stats.formulas.game_score). Flagged only on co-occurrence; see
# module docstring "Bare numeric-literal confinement".
_GAME_SCORE_WEIGHTS = frozenset({0.4, 0.7, 0.3})
_GAME_SCORE_MIN_COOCCURRENCE = 3

# Rule 1c -- eFG%'s half-credit-for-a-three weight
# (app.services.stats.formulas.efg_pct_ratio, app.services.stats.registry.
# efg_pct_num_expr). 0.5 is far too common a float to flag on sight, so it is
# designated only when multiplied by a three-point-makes operand -- the shape
# `0.5 * fg3m` / `fg3m * 0.5`, in Python arithmetic or a SQLAlchemy expression.
#
# Added by the Phase 2 QA gate (#731). T6 bound the Explorer's raw-SQL-text eFG%
# forms to the registry but left its three SQLAlchemy-expression filter sites
# hand-written; rule 1 was blind (0.5 was not designated) and rule 4 was blind
# (it only matches *string* literals), so the engine and the filter could
# silently disagree on the weight. Verified reproducible before this rule
# existed.
_EFG_THREE_POINT_WEIGHT = 0.5
_EFG_THREE_POINT_OPERAND_NAMES = frozenset({"fg3m", "fg_3m", "three_pm", "fg3_made"})

# Rule 2 -- the same designated coefficients, adjacent to `*`, inside string content.
_DESIGNATED_COEFFICIENT_TOKENS = ("0.44", "0.4", "0.7", "0.3")
_COEFF_MULT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _DESIGNATED_COEFFICIENT_TOKENS) + r")\b"
    r"\s*\*"
    r"|"
    r"\*\s*"
    r"\b(?:" + "|".join(re.escape(t) for t in _DESIGNATED_COEFFICIENT_TOKENS) + r")\b"
)

# Keyword arguments that hold formula *prose* next to a MetricDefinition-style
# declaration (app/services/summer_league_environment_registry.py:374-384 is the
# in-repo precedent app.services.stats.registry itself follows). See module
# docstring "Declared formula text is not in scope".
_DECLARATION_PROSE_KEYWORDS = frozenset(
    {
        "formula",
        "denominator",
        "interpretation",
        "interpretation_note",
        "comparison_semantics",
        "minimum_sample_rule",
        "coverage_requirement",
        "exemption_reason",
        "label",
    }
)

# Rule 3 (T10, #741) -- the two canonical ``box`` shapes every ``*_sql_text`` call
# site in this codebase uses: a bare column label (row grain) or a ``SUM(...)``-
# wrapped one (aggregate grain). A handful of call sites additionally wrap in
# ``COALESCE(...)`` for NULL-safety (e.g. Game Score's sort expression) -- that is
# a call-site presentation choice, not part of the metric's canonical text, so it
# is deliberately not included here: this rule exists to catch someone retyping a
# formula from scratch, which reproduces the plain form these two wraps emit, not
# a bespoke NULL-coalescing variant.
_ROW_GRAIN_BOX: Callable[[str], str] = lambda c: c  # noqa: E731
_AGG_GRAIN_BOX: Callable[[str], str] = lambda c: f"SUM({c})"  # noqa: E731

# A registered formula's shortest emitted text is still well past this; guards
# against a pathological future metric with a near-empty SQL form matching
# coincidentally.
_MIN_REGISTRY_FORMULA_TEXT_LENGTH = 8

# How far past a frozen exemption's cited line to look for the literal it
# justifies. See module docstring "Vacuity check" for why this is a window and
# not an exact match.
_EXEMPTION_LINE_TOLERANCE = 5

_CITATION_RE = re.compile(r"(?P<path>[\w][\w./+-]*\.py):(?P<line>\d+)")


@dataclass(frozen=True)
class Violation:
    path: str
    lineno: int
    code: str
    message: str

    def format(self) -> str:
        return f"{self.path}:{self.lineno}: [{self.code}] {self.message}"


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _in_engine_package(rel_path: str) -> bool:
    return rel_path.startswith(_ENGINE_PACKAGE_PREFIX)


def _iter_app_python_files() -> list[Path]:
    """Every ``.py`` file under ``app/`` except the engine package itself."""
    root = REPO_ROOT / "app"
    return [p for p in sorted(root.rglob("*.py")) if not _in_engine_package(_rel(p))]


def _resolve_target_files(argv_paths: list[str]) -> list[Path]:
    if not argv_paths:
        return _iter_app_python_files()
    files: list[Path] = []
    for raw in argv_paths:
        path = Path(raw)
        if path.suffix != ".py" or not path.is_file():
            continue
        resolved = path if path.is_absolute() else (REPO_ROOT / path)
        rel = _rel(resolved)
        if not rel.startswith("app/") or _in_engine_package(rel):
            continue
        files.append(resolved)
    return files


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """Map every node in ``tree`` to its parent node."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Return ``id()`` of every string constant that is a docstring.

    Formula prose in a docstring (e.g. the module docstring at
    ``summer_league_environment_service.py:993``) is documentation, not a runtime
    duplicate. Matches ``scripts/check_runtime_entrypoints.py``'s helper of the
    same name.
    """
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    return docstrings


def _declaration_prose_string_ids(tree: ast.AST) -> set[int]:
    """String/f-string nodes that are declared formula text, not executable SQL.

    The value of a ``formula=``/``denominator=``/... keyword argument in any call --
    see module docstring "Declared formula text is not in scope".
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg in _DECLARATION_PROSE_KEYWORDS:
                ids.add(id(kw.value))
    return ids


def _enclosing_stmt(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    current: ast.AST | None = node
    while current is not None and not isinstance(current, ast.stmt):
        current = parents.get(current)
    return current


def _waived(lines: list[str], node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True if a justified ``# discipline: stat-constants ...`` waiver covers ``node``.

    Scans the enclosing statement's own lines plus the contiguous comment block
    directly above it, matching ``scripts/check_migration_safety.py``'s ``_waived`` --
    a multi-line f-string (the shape the one real violation this checker found took)
    needs more than a single-line lookback.
    """
    statement = _enclosing_stmt(node, parents)
    if isinstance(statement, ast.stmt):
        first, last = statement.lineno, statement.end_lineno or statement.lineno
    else:  # pragma: no cover - every expression sits under some statement
        first = last = getattr(node, "lineno", 1)

    candidates = list(range(first, last + 1))
    cursor = first - 1
    while cursor >= 1 and lines[cursor - 1].lstrip().startswith("#"):
        candidates.append(cursor)
        cursor -= 1

    return any(
        1 <= candidate <= len(lines)
        and line_has_reasoned_waiver(lines[candidate - 1], _RULE)
        for candidate in candidates
    )


def _mult_chain_root(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    """Climb through BinOp/UnaryOp parents to the connected arithmetic root.

    Stops at any other node kind -- a call boundary (``int(x * 0.3)``), a dict
    entry, a statement break -- which is precisely what keeps an isolated scale
    factor (``players * 0.3`` inside an ``int(...)`` call) from being grouped with
    an unrelated coefficient use elsewhere in the same statement. See module
    docstring "Bare numeric-literal confinement".
    """
    current = node
    while True:
        parent = parents.get(current)
        if isinstance(parent, (ast.BinOp, ast.UnaryOp)):
            current = parent
            continue
        return current


def _ts_tov_coefficient_violations(
    rel: str, tree: ast.AST, parents: dict[ast.AST, ast.AST], lines: list[str]
) -> list[Violation]:
    """Rule 1a: the TS%/TOV% free-throw coefficient, flagged unconditionally."""
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Constant)
            and isinstance(node.value, float)
            and node.value == _TS_TOV_COEFFICIENT
        ):
            continue
        if _waived(lines, node, parents):
            continue
        violations.append(
            Violation(
                rel,
                node.lineno,
                "R1",
                f"designated stat coefficient {node.value!r} outside "
                f"{_ENGINE_PACKAGE_PREFIX} (TS%/TOV% free-throw term)",
            )
        )
    return violations


def _game_score_weight_groups(
    tree: ast.AST, parents: dict[ast.AST, ast.AST]
) -> dict[int, list[ast.Constant]]:
    """Group Game Score weight literals by their connected-arithmetic root."""
    groups: dict[int, list[ast.Constant]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, float)):
            continue
        if node.value not in _GAME_SCORE_WEIGHTS:
            continue
        parent = parents.get(node)
        if not (isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Mult)):
            continue
        root = _mult_chain_root(parent, parents)
        groups.setdefault(id(root), []).append(node)
    return groups


def _game_score_weight_violations(
    rel: str, tree: ast.AST, parents: dict[ast.AST, ast.AST], lines: list[str]
) -> list[Violation]:
    """Rule 1b: Game Score weights, flagged only on co-occurrence."""
    violations: list[Violation] = []
    for members in _game_score_weight_groups(tree, parents).values():
        if len(members) < _GAME_SCORE_MIN_COOCCURRENCE:
            continue
        for node in members:
            if _waived(lines, node, parents):
                continue
            violations.append(
                Violation(
                    rel,
                    node.lineno,
                    "R1",
                    f"designated stat coefficient {node.value!r} outside "
                    f"{_ENGINE_PACKAGE_PREFIX} ({len(members)} Game Score weights "
                    "multiplied together in one expression)",
                )
            )
    return violations


def _mentions_three_point_makes(node: ast.AST) -> bool:
    """True if ``node`` reads a three-point-makes field by any of its names.

    Covers the bare name (``fg3m``), the attribute access a SQLAlchemy
    expression uses (``ps.fg3m``), and the ``getattr(table, "fg3m")`` /
    ``func.sum(...)`` wrappers the Explorer's grain indirection builds.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in _EFG_THREE_POINT_OPERAND_NAMES:
            return True
        if (
            isinstance(child, ast.Attribute)
            and child.attr in _EFG_THREE_POINT_OPERAND_NAMES
        ):
            return True
        if (
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and child.value in _EFG_THREE_POINT_OPERAND_NAMES
        ):
            return True
    return False


def _efg_three_point_weight_violations(
    rel: str, tree: ast.AST, parents: dict[ast.AST, ast.AST], lines: list[str]
) -> list[Violation]:
    """Rule 1c: eFG%'s 0.5, flagged only against a three-point-makes operand."""
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Constant)
            and isinstance(node.value, float)
            and node.value == _EFG_THREE_POINT_WEIGHT
        ):
            continue
        parent = parents.get(node)
        if not (isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Mult)):
            continue
        other = parent.right if parent.left is node else parent.left
        if not _mentions_three_point_makes(other):
            continue
        if _waived(lines, node, parents):
            continue
        violations.append(
            Violation(
                rel,
                node.lineno,
                "R1",
                f"designated stat coefficient {node.value!r} outside "
                f"{_ENGINE_PACKAGE_PREFIX} (eFG% three-point half-credit weight; "
                "call app.services.stats.registry.efg_pct_num_expr instead)",
            )
        )
    return violations


def _float_literal_violations(
    rel: str, tree: ast.AST, parents: dict[ast.AST, ast.AST], lines: list[str]
) -> list[Violation]:
    """Rule 1: designated coefficients as bare Python float literals."""
    return (
        _ts_tov_coefficient_violations(rel, tree, parents, lines)
        + _game_score_weight_violations(rel, tree, parents, lines)
        + _efg_three_point_weight_violations(rel, tree, parents, lines)
    )


def _joined_str_formatted_value_text(node: ast.AST) -> str:
    """Render a known f-string column expression as its SQL text equivalent.

    The formula registry's text helpers receive a ``box`` callable, while the
    usual call sites interpolate ``box("field")`` into an f-string. Keeping the
    field name in the joined text lets R4 compare those call sites with the
    registry output instead of silently discarding every formatted expression.
    Unknown expressions remain visibly symbolic rather than becoming false
    literal text.
    """
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in {"box", "getattr"}
            and len(node.args) >= 1
            and isinstance(node.args[-1], ast.Constant)
            and isinstance(node.args[-1].value, str)
        ):
            return node.args[-1].value
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "sum"
            and len(node.args) == 1
        ):
            return f"SUM({_joined_str_formatted_value_text(node.args[0])})"
    return "{" + ast.unparse(node) + "}"


def _joined_str_literal_text(node: ast.JoinedStr) -> str:
    """Concatenate literal and symbolic formatted segments of an f-string."""
    parts: list[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            parts.append(part.value)
        elif isinstance(part, ast.FormattedValue):
            parts.append(_joined_str_formatted_value_text(part.value))
    return "".join(parts)


def _joined_str_child_ids(tree: ast.AST) -> set[int]:
    """``id()`` of every literal-segment ``Constant`` inside a ``JoinedStr``.

    ``ast.walk`` descends into a ``JoinedStr``'s ``.values`` list, so each literal
    segment between interpolations (``" + 0.4 * "``, ``" - 0.7 * "``, ...) is *also*
    a standalone ``ast.Constant`` string node the walk visits on its own. Without this
    exclusion the same f-string is scanned three times over -- once per segment plus
    once for the whole concatenated text -- and reports the same site as multiple
    violations.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    ids.add(id(part))
    return ids


def _string_pattern_violations(
    rel: str, tree: ast.AST, parents: dict[ast.AST, ast.AST], lines: list[str]
) -> list[Violation]:
    """Rule 2: designated-coefficient arithmetic embedded in string/f-string text."""
    docstrings = _docstring_nodes(tree)
    prose = _declaration_prose_string_ids(tree)
    joined_str_children = _joined_str_child_ids(tree)
    violations: list[Violation] = []

    for node in ast.walk(tree):
        text: str
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if (
                id(node) in docstrings
                or id(node) in prose
                or id(node) in joined_str_children
            ):
                continue
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            if id(node) in prose:
                continue
            text = _joined_str_literal_text(node)
        else:
            continue

        match = _COEFF_MULT_RE.search(text)
        if match is None:
            continue
        if _waived(lines, node, parents):
            continue
        violations.append(
            Violation(
                rel,
                node.lineno,
                "R2",
                "stat-aggregate arithmetic "
                f"({match.group(0)!r}) embedded in string/f-string text outside "
                f"{_ENGINE_PACKAGE_PREFIX}",
            )
        )

    return violations


def _registry_sql_text_functions() -> dict[str, Callable[[Callable[[str], str]], str]]:
    """Every ``*_sql_text`` function declared directly in the registry module.

    Discovered by introspection, not a hand-maintained list, so a future metric's
    SQL-text form is covered by this rule the moment it is added to
    :mod:`app.services.stats.registry` -- no companion checker edit required.
    """
    return {
        name: fn
        for name, fn in inspect.getmembers(_stats_registry, inspect.isfunction)
        if name.endswith("_sql_text") and fn.__module__ == _stats_registry.__name__
    }


def _known_registry_formula_texts() -> list[tuple[str, str, str]]:
    """``(function_name, grain, emitted_text)`` for every registered SQL-text form.

    Both canonical ``box`` shapes (see ``_ROW_GRAIN_BOX``/``_AGG_GRAIN_BOX`` above)
    are evaluated for each function, matching the "one declaration, two grains"
    convention every ``*_sql_text`` function in the registry follows.
    """
    texts: list[tuple[str, str, str]] = []
    for name, fn in sorted(_registry_sql_text_functions().items()):
        for grain, box in (("row", _ROW_GRAIN_BOX), ("aggregate", _AGG_GRAIN_BOX)):
            text = fn(box)
            if len(text) >= _MIN_REGISTRY_FORMULA_TEXT_LENGTH:
                texts.append((name, grain, text))
    return texts


# Rule 4 (R5, #745) -- the expression counterpart to R4. The registry's
# ``*_expr`` functions accept a ``box`` callable and return SQLAlchemy arithmetic
# trees. A symbolic probe lets this checker compare the arithmetic shape without
# importing a model table or matching SQLAlchemy's rendered SQL text.
_ExpressionShape = tuple[object, ...]


@dataclass(frozen=True)
class _ExpressionProbe:
    """Symbolic value used to capture a registry expression's arithmetic shape."""

    shape: _ExpressionShape

    @staticmethod
    def _shape(value: object) -> _ExpressionShape:
        if isinstance(value, _ExpressionProbe):
            return value.shape
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return ("constant", value)
        raise TypeError(f"unsupported expression probe operand: {value!r}")

    def _binary(self, operator: str, other: object) -> "_ExpressionProbe":
        left = self.shape
        right = self._shape(other)
        if operator in {"add", "mul"}:
            children = tuple(sorted((left, right), key=repr))
            return _ExpressionProbe((operator, *children))
        return _ExpressionProbe((operator, left, right))

    def _reverse_binary(self, operator: str, other: object) -> "_ExpressionProbe":
        left = self._shape(other)
        right = self.shape
        if operator in {"add", "mul"}:
            children = tuple(sorted((left, right), key=repr))
            return _ExpressionProbe((operator, *children))
        return _ExpressionProbe((operator, left, right))

    def __add__(self, other: object) -> "_ExpressionProbe":
        return self._binary("add", other)

    def __radd__(self, other: object) -> "_ExpressionProbe":
        return self._reverse_binary("add", other)

    def __sub__(self, other: object) -> "_ExpressionProbe":
        return self._binary("sub", other)

    def __rsub__(self, other: object) -> "_ExpressionProbe":
        return self._reverse_binary("sub", other)

    def __mul__(self, other: object) -> "_ExpressionProbe":
        return self._binary("mul", other)

    def __rmul__(self, other: object) -> "_ExpressionProbe":
        return self._reverse_binary("mul", other)

    def __truediv__(self, other: object) -> "_ExpressionProbe":
        return self._binary("div", other)

    def __rtruediv__(self, other: object) -> "_ExpressionProbe":
        return self._reverse_binary("div", other)

    def __neg__(self) -> "_ExpressionProbe":
        return _ExpressionProbe(("neg", self.shape))


def _registry_expression_functions() -> (
    dict[str, Callable[[Callable[[str], _ExpressionProbe]], _ExpressionProbe]]
):
    """Every ``*_expr`` function declared directly in the registry module."""
    return {
        name: fn
        for name, fn in inspect.getmembers(_stats_registry, inspect.isfunction)
        if name.endswith("_expr") and fn.__module__ == _stats_registry.__name__
    }


def _known_registry_expression_shapes() -> list[tuple[str, _ExpressionShape]]:
    """Return registry-derived expression shapes for the R5 AST comparison."""
    shapes: list[tuple[str, _ExpressionShape]] = []
    for name, function in sorted(_registry_expression_functions().items()):
        result = function(lambda field: _ExpressionProbe(("column", field)))
        if not isinstance(result, _ExpressionProbe):
            raise TypeError(
                f"{_stats_registry.__name__}.{name}() did not return an "
                "arithmetic expression probe"
            )
        shapes.append((name, result.shape))
    return shapes


def _func_sum_argument(node: ast.Call) -> ast.AST | None:
    """Return the operand of the ``func.sum(...)`` ORM aggregate wrapper."""
    if (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "func"
        and node.func.attr == "sum"
        and len(node.args) == 1
        and not node.keywords
    ):
        return node.args[0]
    return None


def _getattr_column_name(node: ast.Call) -> str | None:
    """Return a literal column name from ``getattr(table, "column")``."""
    if (
        isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) == 2
        and not node.keywords
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return node.args[1].value
    return None


def _ast_binary_shape(node: ast.BinOp) -> _ExpressionShape | None:
    """Normalize a binary arithmetic AST node to the registry probe shape."""
    operator_names = {
        ast.Add: "add",
        ast.Sub: "sub",
        ast.Mult: "mul",
        ast.Div: "div",
    }
    operator = next(
        (
            name
            for operator_type, name in operator_names.items()
            if isinstance(node.op, operator_type)
        ),
        None,
    )
    if operator is None:
        return None
    left = _ast_expression_shape(node.left)
    right = _ast_expression_shape(node.right)
    if left is None or right is None:
        return None
    if operator in {"add", "mul"}:
        children = tuple(sorted((left, right), key=repr))
        return (operator, *children)
    return (operator, left, right)


def _ast_expression_shape(node: ast.AST) -> _ExpressionShape | None:
    """Normalize a SQLAlchemy arithmetic subtree to the registry probe shape.

    ORM attribute access and the ``func.sum``/``getattr`` forms used to box a
    column at a particular grain are deliberately treated as the same leaf. The
    rule is still structural: arbitrary calls, names, and SQL functions do not
    become leaves, so only arithmetic over recognizable ORM columns can match.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            return None
        return ("constant", node.value)
    if isinstance(node, ast.Attribute):
        return ("column", node.attr)
    if isinstance(node, ast.Call):
        column_name = _getattr_column_name(node)
        if column_name is not None:
            return ("column", column_name)
        sum_argument = _func_sum_argument(node)
        return _ast_expression_shape(sum_argument) if sum_argument is not None else None
    if isinstance(node, ast.BinOp):
        return _ast_binary_shape(node)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        operand = _ast_expression_shape(node.operand)
        return ("neg", operand) if operand is not None else None
    return None


def _registry_expression_reappearance_violations(
    rel: str, tree: ast.AST, parents: dict[ast.AST, ast.AST], lines: list[str]
) -> list[Violation]:
    """Rule 4: a registry-declared SQLAlchemy expression retyped outside the engine."""
    known_shapes = _known_registry_expression_shapes()
    shape_to_name = {shape: name for name, shape in known_shapes}
    violations: list[Violation] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.BinOp, ast.UnaryOp)):
            continue
        shape = _ast_expression_shape(node)
        if shape is None:
            continue
        function_name = shape_to_name.get(shape)
        if function_name is None or _waived(lines, node, parents):
            continue
        violations.append(
            Violation(
                rel,
                node.lineno,
                "R5",
                f"SQLAlchemy expression duplicates app.services.stats.registry."
                f"{function_name}() outside {_ENGINE_PACKAGE_PREFIX} -- import and "
                f"call {function_name} instead of retyping the arithmetic",
            )
        )

    return violations


def _registry_formula_reappearance_violations(
    rel: str, tree: ast.AST, parents: dict[ast.AST, ast.AST], lines: list[str]
) -> list[Violation]:
    """Rule 3: a registered metric's exact SQL text retyped outside the engine.

    Unlike rule 2, this does not require a designated coefficient -- ``fg3ar``/
    ``ftr`` have none -- because it matches the registry's own emitted text, not a
    coefficient pattern. See module docstring "Registry formula-text reappearance".
    """
    docstrings = _docstring_nodes(tree)
    prose = _declaration_prose_string_ids(tree)
    joined_str_children = _joined_str_child_ids(tree)
    known_texts = _known_registry_formula_texts()
    violations: list[Violation] = []

    for node in ast.walk(tree):
        text: str
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if (
                id(node) in docstrings
                or id(node) in prose
                or id(node) in joined_str_children
            ):
                continue
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            if id(node) in prose:
                continue
            text = _joined_str_literal_text(node)
        else:
            continue

        for fn_name, grain, formula_text in known_texts:
            if formula_text not in text:
                continue
            if _waived(lines, node, parents):
                break
            violations.append(
                Violation(
                    rel,
                    node.lineno,
                    "R4",
                    f"raw SQL text duplicates app.services.stats.registry."
                    f"{fn_name}()'s {grain}-grain formula outside "
                    f"{_ENGINE_PACKAGE_PREFIX} -- import and call {fn_name} "
                    "instead of retyping the formula",
                )
            )
            break

    return violations


def find_violations(path: Path, source: str) -> list[Violation]:
    """Return rule-1, rule-2, rule-3, and rule-4 violations for one file's source text.

    ``path`` is reported exactly as given (repo-relative for real files; anything a
    test likes, e.g. ``Path("sample.py")``, for a synthetic source) -- independent of
    where, or whether, ``source`` actually lives on disk. Matches
    ``scripts/check_unscoped_delete.py`` / ``check_migration_safety.py``'s
    ``find_violations(path, source)`` shape so a source string can be fed straight
    from a test's ``dedent(...)`` without touching the filesystem.
    """
    rel = str(path)
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:  # pragma: no cover - pre-commit runs ruff first
        return [Violation(rel, exc.lineno or 0, "R0", f"could not parse ({exc.msg})")]

    parents = _parent_map(tree)
    lines = source.splitlines()
    float_violations = _float_literal_violations(rel, tree, parents, lines)
    float_locations = {
        (violation.path, violation.lineno) for violation in float_violations
    }
    string_violations = _string_pattern_violations(rel, tree, parents, lines)
    string_locations = {
        (violation.path, violation.lineno) for violation in string_violations
    }
    formula_violations = [
        violation
        for violation in _registry_formula_reappearance_violations(
            rel, tree, parents, lines
        )
        if (violation.path, violation.lineno) not in string_locations
    ]
    expression_violations = [
        violation
        for violation in _registry_expression_reappearance_violations(
            rel, tree, parents, lines
        )
        if (violation.path, violation.lineno) not in float_locations
    ]
    return (
        float_violations
        + string_violations
        + formula_violations
        + expression_violations
    )


def find_violations_in_file(path: Path) -> list[Violation]:
    """Read a real, repo-rooted file from disk and return its violations."""
    source = path.read_text(encoding="utf-8", errors="replace")
    return find_violations(Path(_rel(path)), source)


@dataclass(frozen=True)
class _ExemptionSite:
    metric_key: str
    cited_path: str
    cited_line: int
    reason: str


def _parse_exemption_sites() -> tuple[list[_ExemptionSite], list[Violation]]:
    """Read the frozen exemptions from the registry and parse their citations.

    A missing/malformed ``path:line`` citation in ``exemption_reason`` is itself a
    vacuity failure -- an exemption this checker cannot verify is not a usable
    exemption. Returns ``(sites, malformed_violations)``.
    """
    sites: list[_ExemptionSite] = []
    malformed: list[Violation] = []
    for definition in frozen_exemptions():
        reason = definition.exemption_reason or ""
        match = _CITATION_RE.search(reason)
        if match is None:
            malformed.append(
                Violation(
                    "app/services/stats/registry.py",
                    0,
                    "R3",
                    f"frozen exemption {definition.metric_key!r} has no parseable "
                    "'path:line' citation in exemption_reason; cannot verify it "
                    "against the tree it claims to justify",
                )
            )
            continue
        sites.append(
            _ExemptionSite(
                metric_key=definition.metric_key,
                cited_path=match.group("path"),
                cited_line=int(match.group("line")),
                reason=reason,
            )
        )
    return sites, malformed


def _resolve_exemptions(
    sites: list[_ExemptionSite],
) -> tuple[set[tuple[str, int]], list[Violation]]:
    """Match each exemption site against a real rule-1 violation near its cited line.

    Reads the cited file directly rather than relying on whatever this run's target
    files happen to be, so the vacuity check cannot be defeated by a partial
    invocation (see module docstring "Two invocation modes").

    Returns ``(exempted_locations, vacuity_violations)``. ``exempted_locations`` is
    every ``(path, lineno)`` the main sweep should suppress; a site with no match
    within the tolerance window is reported as vacuous, not silently dropped.
    """
    exempted: set[tuple[str, int]] = set()
    vacuous: list[Violation] = []

    for site in sites:
        cited_file = REPO_ROOT / site.cited_path
        if not cited_file.is_file():
            vacuous.append(
                Violation(
                    site.cited_path,
                    site.cited_line,
                    "R3",
                    f"frozen exemption {site.metric_key!r} cites a file that does "
                    "not exist; remove or fix the exemption_reason citation in "
                    "app/services/stats/registry.py",
                )
            )
            continue

        site_violations = find_violations_in_file(cited_file)
        window = range(site.cited_line, site.cited_line + _EXEMPTION_LINE_TOLERANCE + 1)
        matched = [v for v in site_violations if v.code == "R1" and v.lineno in window]
        if not matched:
            vacuous.append(
                Violation(
                    site.cited_path,
                    site.cited_line,
                    "R3",
                    f"frozen exemption {site.metric_key!r} cites "
                    f"{site.cited_path}:{site.cited_line} but no matching "
                    "designated-coefficient literal was found within "
                    f"{_EXEMPTION_LINE_TOLERANCE} lines; the exemption has rotted "
                    "and should be removed from app/services/stats/registry.py, "
                    "or its citation is wrong",
                )
            )
            continue

        for v in matched:
            exempted.add((v.path, v.lineno))

    return exempted, vacuous


def check(argv_paths: list[str]) -> list[Violation]:
    """Run all four rules across the target files, apply exemptions, return findings."""
    files = _resolve_target_files(argv_paths)

    raw_violations: list[Violation] = []
    for path in files:
        raw_violations.extend(find_violations_in_file(path))

    sites, malformed = _parse_exemption_sites()
    exempted, vacuous = _resolve_exemptions(sites)

    violations = [v for v in raw_violations if (v.path, v.lineno) not in exempted]
    violations.extend(malformed)
    violations.extend(vacuous)
    return violations


def main(argv: list[str] | None = None) -> int:
    argv_paths = list(sys.argv[1:] if argv is None else argv)
    violations = check(argv_paths)

    if not violations:
        print(
            "check_stat_constants: OK (designated stat coefficients confined to app/services/stats/)"
        )
        return 0

    print("Stat-constant confinement violations:\n", file=sys.stderr)
    for violation in sorted(violations, key=lambda v: (v.path, v.lineno)):
        print(f"  {violation.format()}", file=sys.stderr)
    print(
        "\nDesignated stat coefficients (0.44, the Game Score weights), any metric "
        "the registry declares as a *_sql_text form (R4), and any registry "
        "*_expr arithmetic shape (R5) -- may "
        "appear only under app/services/stats/. Move the formula into that package "
        "(or call the existing registry function) and import it, or -- if this is "
        "genuinely a one-off -- justify it inline:\n"
        "\n    # discipline: stat-constants <reason>\n"
        "\nSee docs/plans/programmatic-code-discipline.md §1.3.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
