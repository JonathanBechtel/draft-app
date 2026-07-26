"""Guard that import contract 3's hand-maintained module list stays complete.

Failure this descends from
--------------------------
Contract 3 keeps the shared stat engine (``app/services/stats/``) source-agnostic by
forbidding it from importing Summer League. It names the forbidden modules one by one,
because import-linter module expressions match whole dotted segments — ``*`` stands for an
entire segment, so ``app.services.summer_league*`` is not expressible and a real list is the
only option.

As shipped, the list named 11 of 11 service modules but only 2 of 5 schema modules. The
omissions included ``app.schemas.summer_league_metrics``, which holds
``SummerLeaguePlayerSeason``, ``SummerLeagueMetricContext`` and ``SummerLeagueMetricModel`` —
the tables a lifted stat engine is most likely to reach for. The contract would have stayed
green through exactly the coupling it exists to prevent.

A hand-maintained list with no guard is the same drift class as
``player_merge_service``'s child-table list (backlog 4.4), so it gets the same reflective
treatment: the filesystem supplies the universe, and this test enforces that the contract
covers it. See ``docs/plans/programmatic-code-discipline.md`` §3.1.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_NAME = "app.services.stats must not import Summer League"

# Directories whose ``summer_league*`` modules the engine must not import, and the dotted
# prefix each maps to.
_SCANNED_PACKAGES = (
    ("app/services", "app.services"),
    ("app/schemas", "app.schemas"),
)


def _summer_league_modules() -> set[str]:
    """Return the dotted name of every Summer League module the contract should name.

    Packages (``app/services/summer_league/``) are returned as the package itself: naming a
    package in a forbidden contract covers its descendants, so listing submodules would be
    redundant.
    """
    modules: set[str] = set()
    for directory, prefix in _SCANNED_PACKAGES:
        for path in (REPO_ROOT / directory).iterdir():
            if not path.name.startswith("summer_league"):
                continue
            if path.is_dir() and (path / "__init__.py").is_file():
                modules.add(f"{prefix}.{path.name}")
            elif path.suffix == ".py":
                modules.add(f"{prefix}.{path.stem}")
    return modules


def _forbidden_modules() -> set[str]:
    """Return contract 3's ``forbidden_modules`` as configured in pyproject.toml."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = config["tool"]["importlinter"]["contracts"]
    for contract in contracts:
        if contract["name"] == CONTRACT_NAME:
            return set(contract["forbidden_modules"])
    raise AssertionError(f"Contract {CONTRACT_NAME!r} is missing from pyproject.toml")


def test_contract_names_every_summer_league_module() -> None:
    """Adding a Summer League module must not silently escape the stat-engine contract."""
    missing = sorted(_summer_league_modules() - _forbidden_modules())

    assert not missing, (
        "Summer League module(s) absent from import contract 3:\n"
        + "\n".join(f"  {module}" for module in missing)
        + "\n\nAdd them to [tool.importlinter] forbidden_modules in pyproject.toml, or the\n"
        "stat engine may import them and `lint-imports` will still pass.\n"
        "Wildcards cannot express this — import-linter matches whole dotted segments."
    )


def test_contract_does_not_name_modules_that_no_longer_exist() -> None:
    """A stale entry is dead weight that makes the list look more complete than it is."""
    stale = sorted(_forbidden_modules() - _summer_league_modules())

    assert not stale, (
        "Import contract 3 names module(s) that no longer exist:\n"
        + "\n".join(f"  {module}" for module in stale)
        + "\n\nRemove them from pyproject.toml."
    )
