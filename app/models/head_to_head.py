"""Pydantic models for head-to-head comparison responses."""

from typing import Optional

from pydantic import BaseModel, Field

from app.models.fields import MetricCategory


class HeadToHeadPlayer(BaseModel):
    slug: str
    name: str


class HeadToHeadMetric(BaseModel):
    metric: str
    metric_key: str
    unit: str
    raw_value_a: Optional[float] = Field(default=None)
    raw_value_b: Optional[float] = Field(default=None)
    display_value_a: Optional[str] = Field(default=None)
    display_value_b: Optional[str] = Field(default=None)
    lower_is_better: bool = Field(default=False)


class HeadToHeadSimilarity(BaseModel):
    score: float
    overlap_pct: Optional[float] = Field(default=None)


class HeadToHeadResponse(BaseModel):
    category: MetricCategory
    player_a: HeadToHeadPlayer
    player_b: HeadToHeadPlayer
    metrics: list[HeadToHeadMetric] = Field(default_factory=list)
    similarity: Optional[HeadToHeadSimilarity] = Field(default=None)
