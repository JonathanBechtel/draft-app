from typing import Optional
from datetime import datetime, date
from sqlmodel import SQLModel, Field
from sqlalchemy import event

from app.utils.slug import generate_unique_slug_from_connection


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
