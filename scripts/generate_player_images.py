#!/usr/bin/env python3
"""Generate player portrait images using Google Gemini API.

This script generates AI portraits for NBA draft prospects and stores them in S3.
Supports batch generation by cohort, draft year, or individual players.

Usage:
    # Generate for 2025 draft class
    python scripts/generate_player_images.py --draft-year 2025 --run-key "draft_2025_v1"

    # Generate for 2025 draft class via season (more semantic)
    python scripts/generate_player_images.py --season 2024-25 --run-key "draft_2025_v1"

    # Generate for current NBA players, missing images only
    python scripts/generate_player_images.py --cohort current_nba --missing-only

    # Generate for specific player with reference image
    python scripts/generate_player_images.py --player-id 1661 --fetch-likeness

    # Dry run to preview and estimate costs
    python scripts/generate_player_images.py --all --dry-run
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Optional

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.fields import CohortType
from app.schemas.image_snapshots import PlayerImageAsset, PlayerImageSnapshot
from app.schemas.metrics import MetricSnapshot, PlayerMetricValue
from app.schemas.players_master import PlayerMaster
from app.schemas.seasons import Season
from app.services.image_generation import image_generation_service
from app.utils.db_async import SessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Estimated cost per image (for dry run estimates)
COST_PER_IMAGE_USD = {
    "512": 0.02,
    "1K": 0.04,
    "2K": 0.08,
}


def generate_run_key(cohort: str, style: str, draft_year: Optional[int] = None) -> str:
    """Generate a unique run key for this batch."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if draft_year:
        return f"{style}_{cohort}_{draft_year}_{timestamp}"
    return f"{style}_{cohort}_{timestamp}"


async def resolve_season(db: AsyncSession, code: str) -> Season:
    """Resolve a season by code (e.g., '2024-25')."""
    season = (
        await db.execute(select(Season).where(Season.code == code))
    ).scalar_one_or_none()
    if season is None:
        raise ValueError(f"Season {code!r} not found in seasons table")
    if season.id is None:
        raise ValueError(f"Season {code!r} is missing a persisted id")
    return season


async def get_players_for_season(
    db: AsyncSession,
    *,
    season_code: str,
    limit: Optional[int] = None,
) -> list[PlayerMaster]:
    """Fetch players included in current_draft metric snapshots for a season.

    This aligns image generation with the population used by metrics snapshots,
    rather than relying on `players_master.draft_year` tagging.
    """
    season = await resolve_season(db, season_code)
    season_id = season.id
    if season_id is None:
        raise ValueError("season.id is required")

    preferred_snapshots_stmt = (
        select(MetricSnapshot.id)
        .where(
            MetricSnapshot.cohort == CohortType.current_draft,  # type: ignore[arg-type]
            MetricSnapshot.season_id == season_id,
            MetricSnapshot.is_current.is_(True),  # type: ignore[attr-defined]
            MetricSnapshot.position_scope_parent.is_(None),  # type: ignore[union-attr]
            MetricSnapshot.position_scope_fine.is_(None),  # type: ignore[union-attr]
        )
        .order_by(desc(MetricSnapshot.calculated_at))  # type: ignore[arg-type]
    )
    preferred_snapshot_ids = (
        (
            await db.execute(preferred_snapshots_stmt)  # type: ignore[arg-type]
        )
        .scalars()
        .all()
    )

    snapshot_ids = list(preferred_snapshot_ids)
    if not snapshot_ids:
        fallback_stmt = (
            select(MetricSnapshot.id)
            .where(
                MetricSnapshot.cohort == CohortType.current_draft,  # type: ignore[arg-type]
                MetricSnapshot.season_id == season_id,
                MetricSnapshot.is_current.is_(True),  # type: ignore[attr-defined]
            )
            .order_by(desc(MetricSnapshot.calculated_at))  # type: ignore[arg-type]
        )
        snapshot_ids = list(
            (await db.execute(fallback_stmt)).scalars().all()  # type: ignore[arg-type]
        )

    if not snapshot_ids:
        return []

    player_ids_subq = (
        select(PlayerMetricValue.player_id)
        .where(PlayerMetricValue.snapshot_id.in_(snapshot_ids))  # type: ignore[attr-defined]
        .distinct()
        .subquery()
    )

    stmt = (
        select(PlayerMaster)
        .where(PlayerMaster.id.in_(select(player_ids_subq.c.player_id)))  # type: ignore[union-attr]
        .order_by(PlayerMaster.display_name)
    )

    if limit:
        stmt = stmt.limit(limit)

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_players(
    db: AsyncSession,
    player_id: Optional[int] = None,
    player_slug: Optional[str] = None,
    cohort: Optional[CohortType] = None,
    draft_year: Optional[int] = None,
    season: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[PlayerMaster]:
    """Fetch players based on filters.

    Args:
        db: Database session
        player_id: Specific player ID
        player_slug: Specific player slug
        cohort: Filter by cohort type
        draft_year: Filter by draft year
        season: Filter by season code (uses current_draft metric snapshots)
        limit: Maximum number of players

    Returns:
        List of PlayerMaster records
    """
    if season:
        return await get_players_for_season(db, season_code=season, limit=limit)

    stmt = select(PlayerMaster).order_by(PlayerMaster.display_name)

    if player_id:
        stmt = stmt.where(PlayerMaster.id == player_id)
    elif player_slug:
        stmt = stmt.where(PlayerMaster.slug == player_slug)
    else:
        # Apply cohort/draft year filters
        if draft_year:
            stmt = stmt.where(PlayerMaster.draft_year == draft_year)
        elif cohort:
            if cohort == CohortType.current_draft:
                # Players with draft_year = current year or next year
                current_year = datetime.now().year
                stmt = stmt.where(
                    PlayerMaster.draft_year.in_([current_year, current_year + 1])  # type: ignore[union-attr]
                )
            elif cohort == CohortType.current_nba:
                # Players who have debuted
                stmt = stmt.where(PlayerMaster.nba_debut_date.isnot(None))  # type: ignore[union-attr]
            # Add more cohort filters as needed

    if limit:
        stmt = stmt.limit(limit)

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def check_existing_image(
    db: AsyncSession,
    player_id: int,
    style: str,
) -> bool:
    """Check if an image already exists for this player/style combo.

    Args:
        db: Database session
        player_id: Player ID
        style: Image style

    Returns:
        True if image exists in current snapshot
    """
    stmt = (
        select(PlayerImageAsset.id)
        .join(
            PlayerImageSnapshot, PlayerImageSnapshot.id == PlayerImageAsset.snapshot_id
        )
        .where(
            PlayerImageAsset.player_id == player_id,
            PlayerImageSnapshot.style == style,
            PlayerImageSnapshot.is_current == True,  # noqa: E712
            PlayerImageAsset.error_message.is_(None),  # type: ignore[union-attr]
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def demote_current_snapshots(
    db: AsyncSession,
    *,
    style: str,
    cohort: CohortType,
    draft_year: Optional[int],
) -> None:
    """Demote any current snapshot for this context to avoid unique conflicts."""
    stmt = (
        update(PlayerImageSnapshot)
        .where(
            PlayerImageSnapshot.is_current == True,  # noqa: E712
            PlayerImageSnapshot.style == style,
            PlayerImageSnapshot.cohort == cohort,
            (
                PlayerImageSnapshot.draft_year.is_(None)  # type: ignore[union-attr]
                if draft_year is None
                else PlayerImageSnapshot.draft_year == draft_year
            ),
        )
        .values(is_current=False)
    )
    await db.execute(stmt)


async def get_next_version(
    db: AsyncSession,
    style: str,
    cohort: CohortType,
    run_key: str,
) -> int:
    """Get next version number for this snapshot context."""
    stmt = (
        select(PlayerImageSnapshot.version)
        .where(
            PlayerImageSnapshot.style == style,
            PlayerImageSnapshot.cohort == cohort,
            PlayerImageSnapshot.run_key == run_key,
        )
        .order_by(desc(PlayerImageSnapshot.version))  # type: ignore[arg-type]
        .limit(1)
    )
    result = await db.execute(stmt)
    current_max = result.scalar_one_or_none()
    return (current_max or 0) + 1


async def main(args: argparse.Namespace) -> None:
    """Main entry point for image generation."""
    logger.info("Starting player image generation")
    logger.info(f"Args: {args}")

    # Validate args
    if not any(
        [
            args.player_id,
            args.player_slug,
            args.cohort,
            args.draft_year,
            args.season,
            args.all,
        ]
    ):
        logger.error(
            "Must specify --player-id, --player-slug, --cohort, --draft-year, --season, or --all"
        )
        sys.exit(1)

    if args.season and args.draft_year:
        logger.error("Use only one of --season or --draft-year")
        sys.exit(1)

    # Determine cohort
    cohort = CohortType(args.cohort) if args.cohort else CohortType.global_scope
    if args.draft_year or args.season:
        cohort = CohortType.current_draft

    # Generate run key if not provided
    run_key = args.run_key or generate_run_key(
        cohort.value, args.style, args.draft_year
    )
    logger.info(f"Run key: {run_key}")

    async with SessionLocal() as db:
        draft_year: Optional[int] = args.draft_year
        if args.season and draft_year is None:
            season = await resolve_season(db, args.season)
            draft_year = season.end_year

        # Fetch players
        players = await get_players(
            db,
            player_id=args.player_id,
            player_slug=args.player_slug,
            cohort=cohort if not args.draft_year else None,
            draft_year=draft_year,
            season=args.season,
            limit=args.limit,
        )

        if not players:
            logger.warning("No players found matching criteria")
            return

        logger.info(f"Found {len(players)} players")

        # Filter out players with existing images if --missing-only
        if args.missing_only:
            filtered = []
            for player in players:
                has_image = await check_existing_image(db, player.id, args.style)  # type: ignore[arg-type]
                if not has_image:
                    filtered.append(player)
            logger.info(f"After --missing-only filter: {len(filtered)} players")
            players = filtered

        if not players:
            logger.info("No players need image generation")
            return

        # Dry run: just show what would be done
        if args.dry_run:
            cost_estimate = len(players) * COST_PER_IMAGE_USD.get(args.size, 0.04)
            logger.info("=== DRY RUN ===")
            logger.info(f"Would generate {len(players)} images")
            logger.info(f"Style: {args.style}, Size: {args.size}")
            logger.info(f"Estimated cost: ${cost_estimate:.2f}")
            logger.info("Players:")
            for p in players[:10]:
                logger.info(f"  - {p.display_name} (id={p.id}, slug={p.slug})")
            if len(players) > 10:
                logger.info(f"  ... and {len(players) - 10} more")
            return

        # Get system prompt
        system_prompt = image_generation_service.get_system_prompt(args.prompt_version)
        version = await get_next_version(db, args.style, cohort, run_key)

        # Create snapshot record
        snapshot = PlayerImageSnapshot(
            run_key=run_key,
            version=version,
            is_current=False,  # Will set to True after completion
            style=args.style,
            cohort=cohort,
            draft_year=draft_year,
            population_size=len(players),
            image_size=args.size,
            system_prompt=system_prompt,
            system_prompt_version=args.prompt_version,
            notes=args.notes,
            generated_at=datetime.utcnow(),
        )
        db.add(snapshot)
        await db.commit()
        await db.refresh(snapshot)
        logger.info(f"Created snapshot: id={snapshot.id}, version={version}")

        # Generate images
        success_count = 0
        failure_count = 0

        for i, player in enumerate(players, 1):
            logger.info(
                f"[{i}/{len(players)}] Generating image for {player.display_name}"
            )

            try:
                asset = await image_generation_service.generate_for_player(
                    db=db,
                    player=player,
                    snapshot=snapshot,
                    style=args.style,
                    fetch_likeness=args.fetch_likeness,
                    likeness_url=args.likeness_url,
                    image_size=args.size,
                )
                await db.commit()

                if asset.error_message:
                    logger.error(f"  Failed: {asset.error_message}")
                    failure_count += 1
                else:
                    logger.info(f"  Success: {asset.public_url}")
                    success_count += 1

            except Exception as e:
                logger.error(f"  Error: {e}")
                failure_count += 1
                await db.rollback()

        # Update snapshot with final counts
        snapshot.success_count = success_count
        snapshot.failure_count = failure_count
        snapshot.estimated_cost_usd = success_count * COST_PER_IMAGE_USD.get(
            args.size, 0.04
        )

        # Mark as current if all succeeded
        if failure_count == 0 and success_count > 0:
            await demote_current_snapshots(
                db,
                style=args.style,
                cohort=cohort,
                draft_year=args.draft_year,
            )
            snapshot.is_current = True

        await db.commit()

        # Summary
        logger.info("=== GENERATION COMPLETE ===")
        logger.info(f"Success: {success_count}")
        logger.info(f"Failed: {failure_count}")
        logger.info(f"Estimated cost: ${snapshot.estimated_cost_usd:.2f}")
        logger.info(f"Snapshot ID: {snapshot.id}")
        logger.info(f"Is current: {snapshot.is_current}")


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate player portrait images using Gemini API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Player selection (mutually exclusive-ish)
    selection = parser.add_argument_group("Player Selection")
    selection.add_argument(
        "--player-id",
        type=int,
        help="Generate for specific player by ID",
    )
    selection.add_argument(
        "--player-slug",
        type=str,
        help="Generate for specific player by slug",
    )
    selection.add_argument(
        "--cohort",
        type=str,
        choices=[c.value for c in CohortType],
        help="Filter by cohort type",
    )
    selection.add_argument(
        "--draft-year",
        type=int,
        help="Filter by draft year (e.g., 2025)",
    )
    selection.add_argument(
        "--season",
        type=str,
        help="Filter by season code (e.g., 2024-25) using current_draft metric snapshots",
    )
    selection.add_argument(
        "--all",
        action="store_true",
        help="Generate for all players",
    )

    # Generation options
    gen_opts = parser.add_argument_group("Generation Options")
    gen_opts.add_argument(
        "--style",
        type=str,
        default="default",
        help="Image style: default, vector, comic, retro",
    )
    gen_opts.add_argument(
        "--missing-only",
        action="store_true",
        help="Only generate if image doesn't exist",
    )
    gen_opts.add_argument(
        "--fetch-likeness",
        action="store_true",
        help="Enable reference image description for better likeness",
    )
    gen_opts.add_argument(
        "--likeness-url",
        type=str,
        help="Explicit reference image URL (overrides DB)",
    )
    gen_opts.add_argument(
        "--prompt-version",
        type=str,
        default="default",
        help="System prompt version to use",
    )

    # Cost controls
    cost_opts = parser.add_argument_group("Cost Controls")
    cost_opts.add_argument(
        "--size",
        type=str,
        default=settings.image_gen_size,
        choices=["512", "1K", "2K"],
        help="Image size (default from config)",
    )

    # Run controls
    run_opts = parser.add_argument_group("Run Controls")
    run_opts.add_argument(
        "--run-key",
        type=str,
        help="Unique run identifier (auto-generated if not provided)",
    )
    run_opts.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without generating",
    )
    run_opts.add_argument(
        "--limit",
        type=int,
        help="Max players to process (for testing)",
    )
    run_opts.add_argument(
        "--notes",
        type=str,
        help="Notes for this run (stored in snapshot)",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args))
