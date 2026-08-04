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

# Canonical NBA franchise stats team ids (stats.nba.com).
NBA_FRANCHISE_STATS_IDS: frozenset[str] = frozenset(
    {
        "1610612737",  # ATL
        "1610612738",  # BOS
        "1610612739",  # CLE
        "1610612740",  # NOP
        "1610612741",  # CHI
        "1610612742",  # DAL
        "1610612743",  # DEN
        "1610612744",  # GSW
        "1610612745",  # HOU
        "1610612746",  # LAC
        "1610612747",  # LAL
        "1610612748",  # MIA
        "1610612749",  # MIL
        "1610612750",  # MIN
        "1610612751",  # BKN
        "1610612752",  # NYK
        "1610612753",  # ORL
        "1610612754",  # IND
        "1610612755",  # PHI
        "1610612756",  # PHX
        "1610612757",  # POR
        "1610612758",  # SAC
        "1610612759",  # SAS
        "1610612760",  # OKC
        "1610612761",  # TOR
        "1610612762",  # UTA
        "1610612763",  # MEM
        "1610612764",  # WAS
        "1610612765",  # DET
        "1610612766",  # CHA
    }
)

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
