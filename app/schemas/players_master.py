from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional
from datetime import datetime, date
from sqlmodel import SQLModel, Field
from sqlalchemy import Index, event
from sqlalchemy.orm import Session

from app.utils.slug import generate_unique_slug_from_connection

logger = logging.getLogger(__name__)


class PlayerMaster(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "players_master"
    __table_args__ = (
        # Backing index for the Stubs admin tab: WHERE is_stub = true ORDER BY created_at DESC
        Index("ix_players_master_is_stub_created", "is_stub", "created_at"),
    )

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


# A display_name like "ohlbrti01" is a Basketball-Reference id slug with no
# recoverable real name; embedding such rows pollutes vector candidate lists.
_BBREF_SLUG_RE = re.compile(r"^[a-z]+\d{2}$")


def is_embeddable(player: PlayerMaster) -> bool:
    """Whether a player has a real enough name to be a vector-search candidate.

    Excludes rows with an empty ``display_name`` and rows whose ``display_name``
    is a BBRef-style id slug (no recoverable name), so they are never embedded
    and never surface as resolution candidates.
    """
    name = (player.display_name or "").strip()
    if not name:
        return False
    if _BBREF_SLUG_RE.match(name):
        return False
    return True


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


# Key under which newly-inserted PlayerMaster snapshots are stashed on the
# Session until the surrounding transaction commits.
_PENDING_EMBEDDINGS_KEY = "_pending_player_embeddings"


def _schedule_player_embedding(snapshot: dict[str, Any]) -> None:
    """Best-effort: fire-and-forget an embedding write for one player snapshot.

    Spawns an async task that generates the embedding via Gemini and persists
    it in its own session/connection, so a Gemini failure never affects the
    caller. Deliberately swallows all exceptions — a missed embedding is
    recoverable via the backfill script; a broken insert is not.

    Called only from the ``after_commit`` handler below, so the player row is
    already durably committed and visible to the separate session. The FK
    insert therefore cannot race the caller's commit or persist for a row that
    was rolled back.
    """
    player_id = snapshot["player_id"]

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        # No event loop in this thread — skip embedding silently.
        return
    if not loop.is_running():
        # Synchronous / test context without a running loop — skip.
        return

    async def _embed() -> None:
        """Generate and persist the embedding for the committed player."""
        try:
            from app.services.embedding_service import embed_text  # noqa: PLC0415
            from app.schemas.player_embeddings import PlayerEmbedding  # noqa: PLC0415
            from app.config import settings as _settings  # noqa: PLC0415
            from sqlalchemy.ext.asyncio import (  # noqa: PLC0415
                AsyncSession,
                async_sessionmaker,
                create_async_engine,
            )

            parts = [
                part
                for part in (
                    snapshot["display_name"],
                    snapshot["school"],
                    snapshot["birth_country"],
                )
                if part
            ]
            embed_input = " ".join(parts) if parts else (snapshot["display_name"] or "")
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
                        db.add(
                            PlayerEmbedding(
                                player_id=player_id,
                                embedding=vector,
                                model_name=_settings.gemini_embedding_model,
                            )
                        )
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


@event.listens_for(Session, "after_flush")
def collect_inserted_players_for_embedding(
    session: Session,
    flush_context: Any,
) -> None:
    """Snapshot newly-inserted players so they can be embedded after commit.

    Runs inside the flush, where the INSERTs have executed and PKs are
    populated, but defers the embedding write to ``after_commit`` so it never
    races the caller's commit or persists for a row that is later rolled back.
    """
    pending: list[dict[str, Any]] = session.info.setdefault(_PENDING_EMBEDDINGS_KEY, [])
    for obj in session.new:
        if isinstance(obj, PlayerMaster) and obj.id is not None and is_embeddable(obj):
            pending.append(
                {
                    "player_id": obj.id,
                    "display_name": obj.display_name,
                    "school": obj.school,
                    "birth_country": obj.birth_country,
                }
            )


@event.listens_for(Session, "after_commit")
def embed_committed_players(session: Session) -> None:
    """Fire best-effort embedding tasks for players committed in this session."""
    pending = session.info.pop(_PENDING_EMBEDDINGS_KEY, None)
    if not pending:
        return
    for snapshot in pending:
        _schedule_player_embedding(snapshot)


@event.listens_for(Session, "after_rollback")
def discard_uncommitted_player_embeddings(session: Session) -> None:
    """Drop snapshots for inserts that were rolled back — never embed them."""
    session.info.pop(_PENDING_EMBEDDINGS_KEY, None)
