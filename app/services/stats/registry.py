"""SQL forms for the shared stats formulas.

Each builder accepts a ``box`` callable that resolves a neutral field name to a
SQLAlchemy expression.  This keeps one formula definition usable for raw-row and
aggregate queries while leaving ORM table knowledge with the caller.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import func


def scale_sql(num: str, gp: str, seconds: str, pace_seconds: str, mode: str) -> str:
    """Build the SQL form of :func:`app.services.stats.formulas.scale_python`."""
    if mode == "per_game":
        return f"{num} * 1.0 / NULLIF({gp}, 0)"
    if mode == "per_36":
        return f"{num} * 2160.0 / NULLIF({seconds}, 0)"
    if mode == "per_100":
        return f"{num} * 288000.0 / NULLIF({pace_seconds}, 0)"
    return num


def net_rating_expr(box: Callable[[str], Any]) -> Any:
    """Build ``ORtg - DRtg`` from a neutral SQL field resolver."""
    return box("off_rating") - box("def_rating")


def pace_per_48_expr(box: Callable[[str], Any]) -> Any:
    """Build pooled possessions per 48 team minutes."""
    return 48.0 * box("possessions") / func.nullif(box("team_minutes") / 5.0, 0)


def points_per_100_expr(box: Callable[[str], Any]) -> Any:
    """Build points per 100 possessions."""
    return 100.0 * box("points") / func.nullif(box("possessions"), 0)
