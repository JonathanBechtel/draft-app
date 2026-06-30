"""Backfill NBA-CDN reference headshots for resolved Summer League players.

Every resolved Summer League player carries an NBA Stats ``PERSON_ID`` (promoted
into ``player_external_ids`` by the C5 seed). The NBA headshot CDN exposes a free
reference image per PERSON_ID, which is exactly the input the stylized-image
pipeline consumes via :attr:`PlayerMaster.reference_image_url`. This module joins
``players_master`` to its ``nba_stats`` external id and stamps the CDN URL onto
players that lack a reference image, routing 404s / missing ids to a fallback
list for manual or college-headshot sourcing rather than guessing.

The URL validator is injected so the sweep is unit-testable without network and
so callers can opt out of validation for a fast pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.players_master import PlayerMaster

NBA_STATS_SYSTEM = "nba_stats"
NBA_HEADSHOT_CDN_TEMPLATE = (
    "https://cdn.nba.com/headshots/nba/latest/1040x760/{person_id}.png"
)

# Validates that a candidate headshot URL actually resolves to an image. Returns
# True if the URL should be stamped, False to route the player to the fallback.
HeadshotValidator = Callable[[str], Awaitable[bool]]


def nba_headshot_url(nba_stats_person_id: str) -> str:
    """Build the NBA CDN headshot URL for an NBA Stats PERSON_ID.

    Args:
        nba_stats_person_id: The NBA Stats ``PERSON_ID`` (as a string).

    Returns:
        The fully-formed CDN URL for the 1040x760 headshot.
    """
    return NBA_HEADSHOT_CDN_TEMPLATE.format(person_id=nba_stats_person_id)


@dataclass
class HeadshotBackfillReport:
    """Outcome of the headshot backfill sweep.

    Attributes:
        set_count: Players whose ``reference_image_url`` was stamped this run.
        skipped_existing: Players already carrying a reference image (left alone
            unless ``overwrite`` is set).
        fallback: ``(player_id, person_id)`` pairs whose CDN URL failed
            validation — surfaced for manual / college-headshot sourcing.
    """

    set_count: int = 0
    skipped_existing: int = 0
    fallback: list[tuple[int, str]] = field(default_factory=list)


async def backfill_nba_headshots(
    db: AsyncSession,
    *,
    overwrite: bool = False,
    validator: HeadshotValidator | None = None,
) -> HeadshotBackfillReport:
    """Stamp NBA-CDN reference headshots onto resolved players.

    Joins ``players_master`` to its ``nba_stats`` external id (seeded by C5) and,
    for every player lacking a ``reference_image_url`` (or all of them when
    ``overwrite`` is set), builds the CDN URL. When a ``validator`` is supplied,
    a URL that fails validation routes the player to the fallback list instead of
    being stamped. The caller owns the transaction; this only flushes.

    Args:
        db: Async database session.
        overwrite: Re-stamp players that already have a reference image.
        validator: Optional async predicate; ``True`` stamps the URL, ``False``
            routes the player to the fallback. ``None`` skips validation.

    Returns:
        A :class:`HeadshotBackfillReport` summarizing the sweep.
    """
    result = await db.execute(
        select(PlayerMaster, PlayerExternalId.external_id)  # type: ignore[call-overload]
        .join(
            PlayerExternalId,
            PlayerExternalId.player_id == PlayerMaster.id,  # type: ignore[arg-type]
        )
        .where(PlayerExternalId.system == NBA_STATS_SYSTEM)
    )
    report = HeadshotBackfillReport()
    for player, person_id in result.all():
        if player.reference_image_url and not overwrite:
            report.skipped_existing += 1
            continue
        url = nba_headshot_url(str(person_id))
        if validator is not None and not await validator(url):
            if player.id is not None:
                report.fallback.append((player.id, str(person_id)))
            continue
        player.reference_image_url = url
        report.set_count += 1
    await db.flush()
    return report
