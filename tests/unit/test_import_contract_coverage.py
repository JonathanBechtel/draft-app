"""Guard that the hand-maintained Summer League module lists stay complete.

Failure this descends from
--------------------------
Two contracts forbid a package from importing Summer League: contract 3 keeps the shared
stat engine (``app/services/stats/``) source-agnostic, and contract 4 does the same for the
Event Desk. Both name the forbidden modules one by one, because import-linter module
expressions match whole dotted segments — ``*`` stands for an entire segment, so
``app.services.summer_league*`` is not expressible and a real list is the only option.

**Both lists get this guard, not just the first one.** A second copy of a hand-maintained
list is the drift risk doubled, and a contract whose list has rotted reports KEPT while the
coupling it exists to prevent walks straight through.

As shipped, the list named 11 of 11 service modules but only 2 of 5 schema modules. The
omissions included ``app.schemas.summer_league_metrics``, which holds
``SummerLeagueDerivedAgg``, ``SummerLeagueMetricContext`` and ``SummerLeagueMetricModel`` —
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

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

# Every contract that enumerates Summer League modules by hand. Adding a third such contract
# without adding it here would leave it unguarded, so the list is asserted complete below.
ENUMERATED_CONTRACTS = (
    "app.services.stats must not import Summer League",
    "app.services.event_desk must not import Summer League",
)

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


def _contracts() -> list[dict]:
    """Return every configured import-linter contract from pyproject.toml."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(config["tool"]["importlinter"]["contracts"])


def _forbidden_modules(contract_name: str) -> set[str]:
    """Return one contract's ``forbidden_modules`` as configured in pyproject.toml."""
    for contract in _contracts():
        if contract["name"] == contract_name:
            return set(contract["forbidden_modules"])
    raise AssertionError(f"Contract {contract_name!r} is missing from pyproject.toml")


@pytest.mark.parametrize("contract_name", ENUMERATED_CONTRACTS)
def test_contract_names_every_summer_league_module(contract_name: str) -> None:
    """Adding a Summer League module must not silently escape a forbidding contract."""
    missing = sorted(_summer_league_modules() - _forbidden_modules(contract_name))

    assert not missing, (
        f"Summer League module(s) absent from {contract_name!r}:\n"
        + "\n".join(f"  {module}" for module in missing)
        + "\n\nAdd them to [tool.importlinter] forbidden_modules in pyproject.toml, or the\n"
        "guarded package may import them and `lint-imports` will still pass.\n"
        "Wildcards cannot express this — import-linter matches whole dotted segments."
    )


@pytest.mark.parametrize("contract_name", ENUMERATED_CONTRACTS)
def test_contract_does_not_name_modules_that_no_longer_exist(contract_name: str) -> None:
    """A stale entry is dead weight that makes the list look more complete than it is."""
    stale = sorted(_forbidden_modules(contract_name) - _summer_league_modules())

    assert not stale, (
        f"{contract_name!r} names module(s) that no longer exist:\n"
        + "\n".join(f"  {module}" for module in stale)
        + "\n\nRemove them from pyproject.toml."
    )


def test_every_summer_league_enumerating_contract_is_guarded() -> None:
    """A new hand-enumerated contract must be added to ``ENUMERATED_CONTRACTS``.

    Without this, contract 5 or 6 could copy the same list and inherit none of the drift
    protection — the exact way the guard itself goes stale.
    """
    enumerating = {
        contract["name"]
        for contract in _contracts()
        if any(
            module.startswith(("app.services.summer_league", "app.schemas.summer_league"))
            for module in contract.get("forbidden_modules", [])
        )
    }
    unguarded = sorted(enumerating - set(ENUMERATED_CONTRACTS))

    assert not unguarded, (
        "Contract(s) enumerate Summer League modules but are not covered by this test:\n"
        + "\n".join(f"  {name}" for name in unguarded)
        + "\n\nAdd them to ENUMERATED_CONTRACTS."
    )
