"""Guard the `app/cli/` vs `scripts/` boundary documented in CLAUDE.md.

`app/cli/` holds the *shipped runtime jobs* -- entrypoints Fly cron machines
execute inside the deployed container. `scripts/` holds *operator tooling* --
things a developer or a CI runner invokes from a checkout. The split only
buys anything if it is enforced, and it rots quietly: a Fly cron toml gains a
`scripts/...` entrypoint, or a runtime job grows an `import scripts.foo`, and
nothing fails until the day someone tightens `.dockerignore`.

Rules enforced (each violation emits `path:line: [CODE] message`):

  E1  No deployed-container entrypoint may reference `scripts/`. Scans Fly
      cron tomls (`cron = "..."`) and the `flyctl machine run ... --` command
      tails in the deploy workflows. Runtime entrypoints must use the
      `python -m app.cli.<module>` form.

  E2  Nothing under `app/` may `import scripts.*`. The shipped package must
      not depend on operator tooling. Pre-existing violations are listed in
      `_KNOWN_APP_IMPORTS_SCRIPTS` so this check ratchets: the known set may
      shrink, but any *new* violation fails.

Not an import-linter contract (`[tool.importlinter]` in pyproject.toml): E1 is
about Fly tomls and workflow YAML, which import-linter cannot see at all, and
expressing E2 there would mean adding `scripts` to `root_packages`, pulling
~110 operator scripts into the import graph that every other contract is then
evaluated against. The ratcheted allowlist below also lets the one pre-existing
violation stay green, which a plain `forbidden` contract could not.

Usage::

    python scripts/check_runtime_entrypoints.py

Returns a nonzero exit code if any violations were found.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Pre-existing `app/ -> scripts/` imports, recorded so the check can ratchet
# rather than requiring one big refactor up front. Shrink these sets by lifting
# the shared logic into `app/services/` and having both callers import that.
# Never add to them: a new entry means a shipped job just grew a dependency on
# operator tooling.
#
# Baselined per *imported module*, not per file. Exempting a whole file would
# let a fourth `scripts.*` import appear inside an already-listed runtime job
# with the check staying silent -- the exact rot this guard exists to catch.
_KNOWN_APP_IMPORTS_SCRIPTS: dict[str, frozenset[str]] = {
    "app/cli/summer_league_roster_runner.py": frozenset(
        {
            "scripts.bbref_bio_scraper",
            "scripts.fetch_summer_league_rosters",
            "scripts.ingest_player_bios",
        }
    ),
}


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


def cron_line_violates(line: str) -> bool:
    """True if a Fly toml line is a `cron =` entrypoint pointing at `scripts/`.

    Comments are documentation (several tomls quote the historical `flyctl machine
    run` command), so only a live assignment counts.
    """
    stripped = line.strip()
    if stripped.startswith("#"):
        return False
    return bool(re.match(r"^cron\s*=", stripped)) and "scripts/" in stripped


def join_continuations(lines: list[str]) -> list[tuple[int, str]]:
    r"""Fold shell backslash-continuations into logical lines.

    Returns `(lineno, logical_line)` where lineno is where the logical line began.
    A `flyctl machine run` command is normally written across a dozen physical
    lines ending in `\\`; matching only physical lines means the guard depends on
    exactly how the YAML happens to be wrapped, and a harmless reformat silently
    reopens the boundary. Comment lines are dropped before folding.
    """
    logical: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if not buffer:
            start = lineno
        if stripped.endswith("\\"):
            buffer += stripped[:-1].rstrip() + " "
            continue
        logical.append((start, (buffer + stripped).strip()))
        buffer = ""
    if buffer:
        logical.append((start, buffer.strip()))
    return logical


def workflow_line_violates(line: str) -> bool:
    """True if a logical workflow line deploys a `scripts/` path to a Fly machine.

    Two shapes, because the container entrypoint is what matters and not the
    formatting that expresses it:

    * a ``--`` command tail whose next token references ``scripts/`` -- this is
      the entrypoint argv, whether written inline or folded from a continuation;
    * any ``flyctl machine run`` invocation mentioning ``scripts/`` at all, which
      catches orderings the tail pattern alone would miss.

    ``--app``/``--entrypoint`` style flags are not tails: the pattern requires
    whitespace directly after ``--``.
    """
    stripped = line.strip()
    if stripped.startswith("#"):
        return False
    if re.search(r"(?:^|\s)--\s+\S*scripts/", stripped):
        return True
    return "flyctl machine run" in stripped and "scripts/" in stripped


def check_entrypoints() -> list[Violation]:
    """E1 -- no deployed-container entrypoint may reference `scripts/`."""
    violations: list[Violation] = []

    # Fly cron tomls: `cron = "/app/.venv/bin/python ..."`
    for toml_path in sorted((REPO_ROOT / "deploy" / "fly").glob("*.toml")):
        for lineno, line in enumerate(
            toml_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if cron_line_violates(line):
                violations.append(
                    Violation(
                        _rel(toml_path),
                        lineno,
                        "E1",
                        "Fly cron entrypoint references scripts/; runtime jobs "
                        "belong in app/cli/ and must be invoked as "
                        "`python -m app.cli.<module>`.",
                    )
                )

    # Deploy workflows: the `--` command tail of a `flyctl machine run`, folded
    # across backslash-continuations so the check does not depend on YAML wrapping.
    workflows = REPO_ROOT / ".github" / "workflows"
    for wf_path in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
        for lineno, line in join_continuations(
            wf_path.read_text(encoding="utf-8").splitlines()
        ):
            if workflow_line_violates(line):
                violations.append(
                    Violation(
                        _rel(wf_path),
                        lineno,
                        "E1",
                        "flyctl machine entrypoint references scripts/; runtime "
                        "jobs belong in app/cli/ and must be invoked as "
                        "`-- -m app.cli.<module>`.",
                    )
                )

    return violations


def _imports_scripts(tree: ast.AST) -> list[tuple[int, str]]:
    """Return `(lineno, imported_name)` for every `scripts.*` import."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "scripts" or alias.name.startswith("scripts."):
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            # `level > 0` is a relative import, which can never reach scripts/.
            if node.level == 0 and (
                module == "scripts" or module.startswith("scripts.")
            ):
                found.append((node.lineno, module))
    return found


def check_app_imports() -> list[Violation]:
    """E2 -- nothing under `app/` may import from `scripts/` (ratcheted)."""
    violations: list[Violation] = []

    for py_path in sorted((REPO_ROOT / "app").rglob("*.py")):
        rel = _rel(py_path)
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
        except SyntaxError as exc:  # pragma: no cover - defensive
            violations.append(
                Violation(rel, exc.lineno or 0, "E2", f"unparseable: {exc}")
            )
            continue

        # Baselined per imported module: an import this file did not already have
        # is a violation even though the file itself appears in the allowlist.
        allowed = _KNOWN_APP_IMPORTS_SCRIPTS.get(rel, frozenset())
        for lineno, name in _imports_scripts(tree):
            if name in allowed:
                continue
            violations.append(
                Violation(
                    rel,
                    lineno,
                    "E2",
                    f"app/ must not import operator tooling (`{name}`). Lift the "
                    "shared logic into app/services/ and import that from both.",
                )
            )

    # Ratchet hygiene: an allowlisted import that is gone should leave the list,
    # otherwise the exemption silently outlives the dependency it was granted for.
    for known, allowed in sorted(_KNOWN_APP_IMPORTS_SCRIPTS.items()):
        known_path = REPO_ROOT / known
        if not known_path.exists():
            violations.append(
                Violation(
                    known,
                    0,
                    "E2",
                    "listed in _KNOWN_APP_IMPORTS_SCRIPTS but no longer exists; "
                    "remove it from the allowlist.",
                )
            )
            continue
        tree = ast.parse(
            known_path.read_text(encoding="utf-8"), filename=str(known_path)
        )
        still_imported = {name for _, name in _imports_scripts(tree)}
        for stale in sorted(allowed - still_imported):
            violations.append(
                Violation(
                    known,
                    0,
                    "E2",
                    f"no longer imports `{stale}` -- remove it from "
                    "_KNOWN_APP_IMPORTS_SCRIPTS so the boundary stays closed.",
                )
            )

    return violations


def main() -> int:
    violations = check_entrypoints() + check_app_imports()
    if not violations:
        print("check_runtime_entrypoints: OK (app/cli <-> scripts boundary intact)")
        return 0

    print("Runtime entrypoint boundary violations:\n", file=sys.stderr)
    for violation in violations:
        print(f"  {violation.format()}", file=sys.stderr)
    print(
        "\nSee CLAUDE.md -> 'Executable code lives in two places' for the rule.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
