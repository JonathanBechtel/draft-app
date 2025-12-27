"""Pydantic models for player similarity responses."""

from typing import List, Optional

from pydantic import BaseModel, Field

from app.models.fields import SimilarityDimension


class SimilarPlayer(BaseModel):
    """A player similar to the anchor player."""

    id: int
    slug: str
    display_name: str
    position: Optional[str] = Field(default=None)
    school: Optional[str] = Field(default=None)
    draft_year: Optional[int] = Field(default=None)
    similarity_score: float = Field(description="Similarity score 0-100")
    rank: int = Field(description="Rank within anchor's neighbor list")
    shared_position: bool = Field(description="Whether positions match")


class PlayerSimilarityResponse(BaseModel):
    """Response for player similarity endpoint."""

    anchor_slug: str
    dimension: SimilarityDimension
    snapshot_id: Optional[int] = Field(default=None, description="Snapshot ID used")
    players: List[SimilarPlayer] = Field(default_factory=list)
