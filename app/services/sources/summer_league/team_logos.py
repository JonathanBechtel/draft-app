"""Resolve NBA franchise logos for Summer League teams.

Summer League rosters are NBA franchises' summer squads, so each entry's
``nba_stats_team_id`` is the canonical NBA franchise stats id (``1610612xxx``).
That maps directly to the official NBA CDN team logo — including relocated
franchises (Seattle -> OKC, New Jersey -> Brooklyn) that an abbreviation match
would miss. Non-NBA exhibition squads (Orlando White/Blue, national teams) have
non-franchise stats ids and get no logo.

Logos are self-hosted under ``app/static/logos/nba/`` (sourced once from the NBA
CDN) rather than hot-linked, so there is no runtime dependency on an external
host and the stored ``nba_teams.logo_url`` S3 objects (access-denied) are avoided.
"""

from __future__ import annotations

from typing import Optional

from app.services.backbone.team_program_resolution import (
    NBA_STATS_TEAM_ID_TO_ABBREVIATION,
)

# Canonical NBA franchise stats team ids (stats.nba.com), derived from the
# backbone's single copy rather than re-listed here. The backbone resolver
# needs the id -> abbreviation mapping to reach `nba_teams`; this module needs
# only membership. Two hand-maintained copies of the same closed 30-id set can
# drift silently -- a franchise added to one and not the other would give a
# team entry a resolved `team_program_id` but no logo, or the reverse. Deriving
# the frozenset from the dict's keys makes that impossible.
#
# The dependency direction is the legal one: a source spoke may import the
# backbone. Import-linter contract 5 ("spoke independence") forbids the
# reverse -- `app/services/backbone/` importing a Summer League module -- which
# is why the shared constant lives in the backbone and not here.
NBA_FRANCHISE_STATS_IDS: frozenset[str] = frozenset(NBA_STATS_TEAM_ID_TO_ABBREVIATION)

_LOGO_PATH_TEMPLATE = "/static/logos/nba/{stats_id}.svg"


def franchise_logo_url(nba_stats_team_id: Optional[str]) -> Optional[str]:
    """Return the franchise logo path for a stats id, else ``None``.

    Args:
        nba_stats_team_id: A Summer League team entry's ``nba_stats_team_id``.

    Returns:
        The self-hosted logo path when the id is a franchise; ``None`` for
        exhibition squads or a missing id.
    """
    if nba_stats_team_id and nba_stats_team_id in NBA_FRANCHISE_STATS_IDS:
        return _LOGO_PATH_TEMPLATE.format(stats_id=nba_stats_team_id)
    return None
