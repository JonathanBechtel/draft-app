from __future__ import annotations

import asyncio
import logging
from typing import Optional
from datetime import datetime, date
from sqlmodel import SQLModel, Field
from sqlalchemy import event

from app.utils.slug import generate_unique_slug_from_connection

logger = logging.getLogger(__name__)


class PlayerMaster(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "players_master"

    id: Optional[int] = Field(default=None, primary_key=True)
    slug: Optional[str] = Field(default=None, unique=True, index=True)

    prefix: Optional[str] = Field(default=None)
    first_name: Optional[str] = Field(default=None, index=True)
    middle_name: Optional[str] = Field(default=None)
    last_name: Optional[str] = Field(default=None, index=True)
    suffix: Optional[str] = Field(default=None)

    display_name: Optional[str] = Field(default=None, index=True)
    birthdate: Optional[date] = Field(default=None)

    # Immutable biographical facts
    birth_city: Optional[str] = Field(default=None, index=True)
    birth_state_province: Optional[str] = Field(default=None, index=True)
    birth_country: Optional[str] = Field(default=None, index=True)

    school: Optional[str] = Field(default=None, description="College/School")
    school_raw: Optional[str] = Field(
        default=None,
        description="Original school value before canonicalization",
    )
    high_school: Optional[str] = Field(default=None)
    shoots: Optional[str] = Field(default=None, description="Shooting hand")

    # Draft facts
    draft_year: Optional[int] = Field(default=None, index=True)
    draft_round: Optional[int] = Field(default=None)
    draft_pick: Optional[int] = Field(default=None)
    draft_team: Optional[str] = Field(default=None)

    # NBA debut facts
    nba_debut_date: Optional[date] = Field(default=None)
    nba_debut_season: Optional[str] = Field(default=None, index=True)

    # Stub flag: auto-created players with just a name, pending enrichment
    is_stub: bool = Field(default=False)

    # Recruiting rank (RSCI composite, if available)
    rsci_rank: Optional[int] = Field(default=None)

    # Enrichment tracking
    bio_source: Optional[str] = Field(
        default=None,
        description="How bio data was populated: 'manual', 'ai_generated', 'verified'",
    )
    enrichment_attempted_at: Optional[datetime] = Field(default=None)

    # Image generation
    reference_image_url: Optional[str] = Field(
        default=None,
        description="URL to reference image for AI likeness generation",
    )
    reference_image_s3_key: Optional[str] = Field(
        default=None,
        description="S3 key for uploaded reference image (private bucket)",
    )

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


@event.listens_for(PlayerMaster, "before_insert")
def generate_slug_before_insert(
    mapper,  # type: ignore[no-untyped-def]
    connection,  # type: ignore[no-untyped-def]
    target: PlayerMaster,
) -> None:
    """Auto-generate slug from display_name if not provided.

    Handles collisions by appending numeric suffix (-2, -3, etc.).
    """
    if target.slug is not None:
        return  # Slug already set, don't override

    if not target.display_name:
        return  # No display_name to generate from

    target.slug = generate_unique_slug_from_connection(
        target.display_name,
        connection,
    )


@event.listens_for(PlayerMaster, "after_insert")
def schedule_embedding_after_insert(
    mapper,  # type: ignore[no-untyped-def]
    connection,  # type: ignore[no-untyped-def]
    target: PlayerMaster,
) -> None:
    """Best-effort: enqueue an embedding write after a PlayerMaster insert.

    The embedding is generated and written asynchronously so that a Gemini
    API failure (network error, quota, key not configured) never blocks the
    insert transaction.  The embed result is persisted in a separate
    session/connection to avoid touching the caller's open transaction.

    SQLAlchemy ``after_insert`` fires *inside* the flush but *before* the
    caller's ``commit()``, so ``target.id`` is already populated.

    Note:
        This listener deliberately swallows all exceptions.  A failed
        embedding is recoverable via the backfill script; a broken insert
        is not.
    """
    player_id = target.id
    if player_id is None:
        # No PK yet — cannot write the FK row; skip silently.
        return

    # Capture a lightweight snapshot of the fields we need so we don't hold a
    # reference to the SQLAlchemy-managed ``target`` across async boundaries.
    display_name = target.display_name
    school = target.school
    birth_country = target.birth_country

    def _fire_and_forget() -> None:
        """Spawn the async embedding task in a best-effort fire-and-forget."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            # No event loop in this thread — skip embedding silently.
            return

        if not loop.is_running():
            # Synchronous / test context without a running loop — skip.
            return

        async def _embed() -> None:
            """Generate and persist the embedding for the newly inserted player."""
            try:
                from app.services.embedding_service import embed_text  # noqa: PLC0415
                from app.schemas.player_embeddings import PlayerEmbedding  # noqa: PLC0415
                from app.config import settings as _settings  # noqa: PLC0415
                from sqlalchemy.ext.asyncio import (  # noqa: PLC0415
                    AsyncSession,
                    async_sessionmaker,
                    create_async_engine,
                )

                # Build embed input from the captured snapshot.
                parts: list[str] = []
                if display_name:
                    parts.append(display_name)
                if school:
                    parts.append(school)
                if birth_country:
                    parts.append(birth_country)
                embed_input = " ".join(parts) if parts else (display_name or "")
                if not embed_input.strip():
                    return

                vector = await embed_text(embed_input)

                engine = create_async_engine(_settings.database_url, echo=False)
                try:
                    factory = async_sessionmaker(
                        bind=engine, expire_on_commit=False, class_=AsyncSession
                    )
                    async with factory() as db:
                        async with db.begin():
                            embedding_row = PlayerEmbedding(
                                player_id=player_id,
                                embedding=vector,
                                model_name=_settings.gemini_embedding_model,
                            )
                            db.add(embedding_row)
                finally:
                    # Always dispose, even if embed/insert raised, so per-insert
                    # engines never leak pooled connections in long-running workers.
                    await engine.dispose()
            except Exception:
                logger.debug(
                    "Best-effort embedding skipped for player_id=%s",
                    player_id,
                    exc_info=True,
                )

        asyncio.ensure_future(_embed())

    _fire_and_forget()
