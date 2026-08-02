"""Response shapes for the scope-parameterized Summer League trend API."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class TrendCohortBand(BaseModel):
    """Median and interquartile band for one metric/day cohort."""

    median: float
    q1: float
    q3: float


class TrendPoint(BaseModel):
    """One cumulative-through-day metric value and its cohort context."""

    model_config = ConfigDict(extra="forbid")

    metric_key: str
    effective_day: date
    value: float
    cohort_band: TrendCohortBand
    as_of: datetime | None = None
