"""Pydantic models for metric responses."""

from typing import List, Optional

from pydantic import BaseModel, Field


class PlayerMetricItem(BaseModel):
    metric: str
    value: Optional[str] = Field(default=None)
    percentile: Optional[int] = Field(default=None)
    unit: str = Field(default="")
    rank: Optional[int] = Field(default=None)
    population_size: Optional[int] = Field(
        default=None, description="Population size used for this metric's percentile"
    )


class CombineScoreCategoryPayload(BaseModel):
    key: str
    label: str
    percentile: float
    color: str


class CombineScorePayload(BaseModel):
    overall_percentile: float
    overall_rank: int
    grade: str
    population_size: Optional[int] = None
    categories: List[CombineScoreCategoryPayload] = Field(default_factory=list)


class PlayerMetricsResponse(BaseModel):
    snapshot_id: Optional[int] = Field(
        default=None, description="MetricSnapshot.id used to source these metrics"
    )
    population_size: Optional[int] = Field(
        default=None, description="Population size for the cohort/snapshot"
    )
    metrics: List[PlayerMetricItem] = Field(default_factory=list)
    combine_score: Optional[CombineScorePayload] = Field(
        default=None, description="Composite combine score for the same cohort/scope"
    )
