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
# rather than requiring one big refactor up front. Shrink this set by lifting
# the shared logic into `app/services/` and having both callers import that.
# Never add to it: a new entry means a shipped job just grew a dependency on
# operator tooling.
_KNOWN_APP_IMPORTS_SCRIPTS: frozenset[str] = frozenset(
    {
        "app/cli/summer_league_roster_runner.py",
    }
)


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


def workflow_line_violates(line: str) -> bool:
    """True if a workflow line is a `flyctl machine run ... --` tail using `scripts/`."""
    stripped = line.strip()
    if stripped.startswith("#"):
        return False
    return bool(re.match(r"^--\s+\S", stripped)) and "scripts/" in stripped


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

    # Deploy workflows: the `--` command tail of a `flyctl machine run`.
    workflows = REPO_ROOT / ".github" / "workflows"
    for wf_path in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
        for lineno, line in enumerate(
            wf_path.read_text(encoding="utf-8").splitlines(), start=1
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

        offenders = _imports_scripts(tree)
        if offenders and rel not in _KNOWN_APP_IMPORTS_SCRIPTS:
            for lineno, name in offenders:
                violations.append(
                    Violation(
                        rel,
                        lineno,
                        "E2",
                        f"app/ must not import operator tooling (`{name}`). Lift the "
                        "shared logic into app/services/ and import that from both.",
                    )
                )

    # Ratchet hygiene: a file that no longer offends should leave the allowlist.
    for known in sorted(_KNOWN_APP_IMPORTS_SCRIPTS):
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
        if not _imports_scripts(tree):
            violations.append(
                Violation(
                    known,
                    0,
                    "E2",
                    "no longer imports scripts/ -- remove it from "
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
