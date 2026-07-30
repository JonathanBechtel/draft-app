"""Unit tests for the operator board writer's identity guard preflight."""

from __future__ import annotations

import pytest

from app.schemas.boards import ResolutionMethod
from app.services.player_identity_guard import IdentityVariantIndex
from scripts.ingest_prospects_concepts_board import _prepare_guarded_plan


def test_guarded_plan_reuses_safe_existing_identity() -> None:
    """A diacritic-only existing identity becomes an exact board entry."""
    index = IdentityVariantIndex()
    index.add_display_name(9, "José García")
    plan = [(1, 1, "Jose Garcia", None, ResolutionMethod.STUB, "STUB")]

    guarded = _prepare_guarded_plan(
        plan,
        index,
        stub_method=ResolutionMethod.STUB,
        exact_method=ResolutionMethod.EXACT,
        alias_method=ResolutionMethod.ALIAS,
    )

    assert guarded == [
        (
            1,
            1,
            "Jose Garcia",
            9,
            ResolutionMethod.EXACT,
            "EXACT -> #9 (reused existing player)",
        )
    ]


def test_guarded_plan_rejects_suffix_mismatch_before_writes() -> None:
    """A suffix mismatch aborts before the script can clear the board."""
    index = IdentityVariantIndex()
    index.add_display_name(7, "Gary Payton II")
    plan = [(1, 1, "Gary Payton", None, ResolutionMethod.STUB, "STUB")]

    with pytest.raises(SystemExit, match="suffix_mismatch"):
        _prepare_guarded_plan(
            plan,
            index,
            stub_method=ResolutionMethod.STUB,
            exact_method=ResolutionMethod.EXACT,
            alias_method=ResolutionMethod.ALIAS,
        )
