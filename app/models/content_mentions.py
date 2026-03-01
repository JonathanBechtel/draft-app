"""Shared request/response models for polymorphic content mentions."""

from sqlmodel import SQLModel


class MentionedPlayer(SQLModel):
    """Lightweight player reference attached to content cards."""

    player_id: int
    display_name: str
    slug: str
