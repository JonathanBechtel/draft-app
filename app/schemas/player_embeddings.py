"""SQLModel table for player vector embeddings.

Stores 768-dimensional Gemini text-embedding-004 vectors for each player,
enabling vector-search-based entity resolution (fuzzy name matching).
"""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, ForeignKey, Integer, Text
from sqlmodel import Field, SQLModel


class PlayerEmbedding(SQLModel, table=True):  # type: ignore[call-arg]
    """A single player's vector embedding produced by an embedding model.

    The table has a 1:1 relationship with ``players_master`` — one row per
    player — with ``player_id`` as both PK and FK so deleting the player
    cascades automatically.

    Attributes:
        player_id: Primary key and FK to ``players_master.id``.
        embedding: 768-dimensional float vector from the embedding model.
        model_name: Identifier of the model that produced the vector (e.g.
            ``"text-embedding-004"``). Stored so rows can be re-embedded when
            the model changes.
        created_at: Timestamp of initial embedding creation.
        updated_at: Timestamp of last embedding update.
    """

    __tablename__ = "player_embeddings"

    player_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("players_master.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        )
    )

    # Vector column — SQLModel has no native Vector type; use sa_column.
    embedding: list[float] = Field(
        sa_column=Column(
            Vector(768),
            nullable=False,
        )
    )

    model_name: str = Field(sa_column=Column(Text, nullable=False))

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
