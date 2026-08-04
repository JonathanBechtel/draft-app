"""Unit tests for the ``app.domain.temporal`` value objects.

Covers construction, immutability, the nullable ``Watermark.source_as_of``
case (P4), and a belt-and-braces static check that the module stays free of
``app.schemas`` / ``app.services`` imports alongside the enforced import
contract (``pyproject.toml``, contract 2).
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from app.domain.temporal import Scope, VersionStamps, Watermark


def test_watermark_constructs_with_all_fields_populated() -> None:
    """A fully-populated Watermark stores its currency, build time, and version."""
    built_at = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    source_at = datetime(2026, 8, 1, 6, tzinfo=timezone.utc)

    watermark = Watermark(
        source_as_of=source_at, projection_built_at=built_at, projection_version=3
    )

    assert watermark.source_as_of == source_at
    assert watermark.projection_built_at == built_at
    assert watermark.projection_version == 3


def test_watermark_source_as_of_none_is_constructible() -> None:
    """P4: a watermark that cannot state its currency must be representable."""
    watermark = Watermark(
        source_as_of=None, projection_built_at=None, projection_version=None
    )

    assert watermark.source_as_of is None
    assert watermark.projection_built_at is None
    assert watermark.projection_version is None


def test_watermark_is_frozen() -> None:
    """Watermark is a value object; corrections produce new instances, not mutation."""
    watermark = Watermark(
        source_as_of=None, projection_built_at=None, projection_version=None
    )

    with pytest.raises(FrozenInstanceError):
        watermark.source_as_of = datetime(2026, 1, 1)  # type: ignore[misc]


def test_version_stamps_constructs_and_keeps_three_stamps_distinct() -> None:
    """The three stamps are independent fields, never collapsed into one."""
    stamps = VersionStamps(
        version=5, registry_version="registry-v2", calculation_version="calc-v1"
    )

    assert stamps.version == 5
    assert stamps.registry_version == "registry-v2"
    assert stamps.calculation_version == "calc-v1"


def test_version_stamps_is_frozen() -> None:
    """VersionStamps is immutable."""
    stamps = VersionStamps(
        version=1, registry_version="registry-v1", calculation_version="calc-v1"
    )

    with pytest.raises(FrozenInstanceError):
        stamps.version = 2  # type: ignore[misc]


def test_scope_constructs_with_key_and_kind() -> None:
    """Scope carries a stable key plus its kind, matching the trend read's usage."""
    scope = Scope(scope_key="competition:42", scope_kind="competition")

    assert scope.scope_key == "competition:42"
    assert scope.scope_kind == "competition"


def test_scope_is_frozen() -> None:
    """Scope is immutable."""
    scope = Scope(scope_key="season:2026", scope_kind="season")

    with pytest.raises(FrozenInstanceError):
        scope.scope_key = "season:2027"  # type: ignore[misc]


def test_temporal_module_imports_nothing_from_schemas_services_or_routes() -> None:
    """Belt-and-braces check alongside import-linter contract 2.

    Parses the module source directly (rather than relying only on the
    contract-checker running separately) so a regression here fails the same
    test suite that exercises the types.
    """
    import app.domain.temporal as temporal_module

    source = inspect.getsource(temporal_module)
    tree = ast.parse(source)

    forbidden_prefixes = ("app.schemas", "app.services", "app.routes")
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    offending = [
        name
        for name in imported_modules
        if name.startswith(forbidden_prefixes)
    ]
    assert offending == []
