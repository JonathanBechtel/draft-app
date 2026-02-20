"""Permission management for staff users."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.auth import AuthDatasetPermission

# Known datasets that can have permissions assigned
KNOWN_DATASETS = [
    "news_sources",
    "news_ingestion",
    "players",
    "images",
    "podcasts",
    "podcast_ingestion",
]


@dataclass
class DatasetPermission:
    """Permission state for a single dataset."""

    dataset: str
    can_view: bool
    can_edit: bool


async def get_user_permissions(
    db: AsyncSession,
    *,
    user_id: int,
) -> list[DatasetPermission]:
    """Get all permissions for a user.

    Args:
        db: Database session.
        user_id: The ID of the user.

    Returns:
        List of DatasetPermission for all known datasets.
        Datasets without explicit permissions default to no access.
    """
    async with db.begin():
        result = await db.execute(
            select(AuthDatasetPermission).where(
                AuthDatasetPermission.user_id == user_id  # type: ignore[arg-type]
            )
        )
        existing = {p.dataset: p for p in result.scalars().all()}

    permissions = []
    for dataset in KNOWN_DATASETS:
        if dataset in existing:
            perm = existing[dataset]
            permissions.append(
                DatasetPermission(
                    dataset=dataset,
                    can_view=perm.can_view,
                    can_edit=perm.can_edit,
                )
            )
        else:
            permissions.append(
                DatasetPermission(
                    dataset=dataset,
                    can_view=False,
                    can_edit=False,
                )
            )

    return permissions


async def set_user_permissions(
    db: AsyncSession,
    *,
    user_id: int,
    permissions: list[DatasetPermission],
) -> None:
    """Replace permissions for a user with a new set.

    Args:
        db: Database session.
        user_id: The ID of the user.
        permissions: List of DatasetPermission to set.
    """
    now = datetime.now(UTC).replace(tzinfo=None)

    async with db.begin():
        # Delete all existing permissions for this user
        await db.execute(
            delete(AuthDatasetPermission).where(
                AuthDatasetPermission.user_id == user_id  # type: ignore[arg-type]
            )
        )

        # Insert new permissions (only if at least one flag is true)
        for perm in permissions:
            if perm.can_view or perm.can_edit:
                db.add(
                    AuthDatasetPermission(
                        user_id=user_id,
                        dataset=perm.dataset,
                        can_view=perm.can_view,
                        can_edit=perm.can_edit,
                        created_at=now,
                        updated_at=now,
                    )
                )
