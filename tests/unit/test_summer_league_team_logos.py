"""Unit tests for Summer League franchise logo resolution."""

from __future__ import annotations

from app.services.sources.summer_league.team_logos import franchise_logo_url


def test_franchise_logo_url_for_franchise() -> None:
    """A franchise stats id resolves to its NBA CDN logo (incl. relocations)."""
    # Charlotte (current) and Brooklyn (relocated from New Jersey) franchise ids.
    assert franchise_logo_url("1610612766") == "/static/logos/nba/1610612766.svg"
    assert franchise_logo_url("1610612751") == "/static/logos/nba/1610612751.svg"


def test_franchise_logo_url_for_non_franchise() -> None:
    """Exhibition squads and missing ids resolve to None."""
    assert franchise_logo_url("1612709900") is None  # exhibition squad
    assert franchise_logo_url("") is None
    assert franchise_logo_url(None) is None
