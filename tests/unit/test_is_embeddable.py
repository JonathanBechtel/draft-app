"""Tests for the embeddability guard that keeps nameless rows out of candidates."""

import pytest

from app.schemas.players_master import PlayerMaster, is_embeddable


@pytest.mark.parametrize(
    "display_name",
    ["ohlbrti01", "yangha01", "pendeje02", "abdulma01", "mbengdj01"],
)
def test_bbref_slug_names_are_not_embeddable(display_name: str) -> None:
    """BBRef-id-slug display_names carry no real name and must be excluded."""
    assert is_embeddable(PlayerMaster(id=1, display_name=display_name)) is False


@pytest.mark.parametrize("display_name", ["", "   ", None])
def test_empty_display_name_is_not_embeddable(display_name: str | None) -> None:
    """Rows with no usable display_name are excluded."""
    assert is_embeddable(PlayerMaster(id=1, display_name=display_name)) is False


@pytest.mark.parametrize(
    "display_name",
    [
        "Aday Mara",
        "Cooper Flagg",
        "Morez Johnson Jr.",
        "A.J. Dybantsa",
        "Nene",  # single real name, not a slug
        "Théo Maledon",
    ],
)
def test_real_names_are_embeddable(display_name: str) -> None:
    """Genuine player names — including single names and diacritics — are kept."""
    assert is_embeddable(PlayerMaster(id=1, display_name=display_name)) is True
