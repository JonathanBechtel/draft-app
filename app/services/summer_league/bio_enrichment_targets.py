"""Bio-enrichment target selection for the Summer League rostered cohort.

Restricts ``scripts/bbref_bio_scraper.py`` and ``scripts/ingest_player_bios.py``
to the SL rostered cohort (see :mod:`app.services.summer_league.cohort`) that
already has a resolved ``bbref`` external id. Scheduled roster runs can force
changed players while retrying cohort players that have never completed a BBR
enrichment. Cohort players without a bbref id cannot be safely scraped/matched
by slug, so they are reported to a manual-review list instead of being errored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_bio_snapshots import PlayerBioSnapshot
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.player_status import PlayerStatus
from app.services.summer_league.cohort import summer_league_cohort

SYSTEM_BBR = "bbr"


@dataclass
class BioEnrichmentTargets:
    """The bio-enrichment target set for a Summer League cohort scope.

    Attributes:
        slugs: BBRef slugs to scrape/ingest for cohort players that already
            have a resolved ``bbref`` external id.
        player_id_by_slug: Map of bbref slug -> canonical ``player_id``.
        manual_review_player_ids: Cohort ``player_id``s with no ``bbref``
            external id -- flagged for manual matching, not errored.
    """

    slugs: set[str] = field(default_factory=set)
    player_id_by_slug: dict[str, int] = field(default_factory=dict)
    manual_review_player_ids: set[int] = field(default_factory=set)


async def select_bio_enrichment_targets(  # noqa: PLR0913
    db: AsyncSession,
    *,
    year: Optional[int] = None,
    league_id: Optional[str] = None,
    venue_slug: Optional[str] = None,
    player_ids: Optional[set[int]] = None,
    retry_unenriched: bool = False,
) -> BioEnrichmentTargets:
    """Restrict the bio-enrichment target set to the SL cohort with a bbref id.

    Args:
        db: Active async session.
        year: Optional competition year filter, forwarded to
            ``summer_league_cohort``.
        league_id: Optional NBA.com ``LeagueID`` filter.
        venue_slug: Optional venue slug filter.
        player_ids: Optional canonical player IDs whose roster change should force
            enrichment.
        retry_unenriched: When ``True``, include cohort players without a
            successful BBR enrichment so failed first attempts remain retryable.

    Returns:
        A :class:`BioEnrichmentTargets` with the bbref slugs to enrich and
        the cohort ``player_id``s that have no bbref id (manual-review list).
    """
    cohort = await summer_league_cohort(
        db, year=year, league_id=league_id, venue_slug=venue_slug
    )
    if not cohort.player_ids:
        return BioEnrichmentTargets()

    if player_ids is None:
        target_player_ids = set(cohort.player_ids)
    elif retry_unenriched:
        successful_status_res = await db.execute(
            select(PlayerStatus.player_id).where(  # type: ignore[call-overload]
                PlayerStatus.player_id.in_(cohort.player_ids),  # type: ignore[attr-defined]
                PlayerStatus.source == SYSTEM_BBR,
            )
        )
        successful_snapshot_res = await db.execute(
            select(PlayerBioSnapshot.player_id).where(  # type: ignore[call-overload]
                PlayerBioSnapshot.player_id.in_(cohort.player_ids),  # type: ignore[attr-defined]
            )
        )
        successfully_enriched = {
            player_id for (player_id,) in successful_status_res.all()
        }
        successfully_enriched.update(
            player_id for (player_id,) in successful_snapshot_res.all()
        )
        target_player_ids = (player_ids & cohort.player_ids) | (
            cohort.player_ids - successfully_enriched
        )
    else:
        target_player_ids = player_ids & cohort.player_ids

    ext_res = await db.execute(
        select(  # type: ignore[call-overload]
            PlayerExternalId.player_id, PlayerExternalId.external_id
        ).where(
            PlayerExternalId.system == SYSTEM_BBR,
            PlayerExternalId.player_id.in_(cohort.player_ids),  # type: ignore[attr-defined]
        )
    )
    player_id_by_slug: dict[str, int] = {}
    matched_player_ids: set[int] = set()
    for player_id, external_id in ext_res.all():
        matched_player_ids.add(player_id)
        if player_id in target_player_ids:
            player_id_by_slug[external_id] = player_id

    manual_review = cohort.player_ids - matched_player_ids
    return BioEnrichmentTargets(
        slugs=set(player_id_by_slug.keys()),
        player_id_by_slug=player_id_by_slug,
        manual_review_player_ids=manual_review,
    )
