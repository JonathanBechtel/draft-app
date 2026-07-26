"""Import ``scripts/*.py`` helpers by path for testing.

``scripts/`` holds standalone entry points that pre-commit runs as files, not as an
installed package, so tests cannot simply import them. Loading by path also has to put
``scripts/`` on ``sys.path`` first: the checkers import their shared waiver vocabulary
from ``_discipline`` as a sibling module, which resolves when pre-commit runs them
directly (``sys.path[0]`` is the script's own directory) but not when pytest imports them
from the repo root.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def load_script(name: str) -> ModuleType:
    """Import ``scripts/<name>.py`` and return the module.

    Args:
        name: Module name without the ``.py`` suffix, e.g. ``"check_unscoped_delete"``.
    """
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"could not build an import spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
