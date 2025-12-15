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


class PlayerMetricsResponse(BaseModel):
    snapshot_id: Optional[int] = Field(
        default=None, description="MetricSnapshot.id used to source these metrics"
    )
    population_size: Optional[int] = Field(
        default=None, description="Population size for the cohort/snapshot"
    )
    metrics: List[PlayerMetricItem] = Field(default_factory=list)
