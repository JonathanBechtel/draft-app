"""Contains Pydantic fields to be used in various models."""

from enum import Enum
from datetime import date
from typing import Annotated
from pydantic import Field as PydField


class Position(str, Enum):
    g = "guard"
    f = "forward"
    c = "center"


BIRTH_DATE = Annotated[date, PydField(..., ge=date(1980, 1, 1))]


class MetricSource(str, Enum):
    combine_agility = "combine_agility"
    combine_anthro = "combine_anthro"
    combine_shooting = "combine_shooting"
    advanced_stats = "advanced_stats"


class MetricStatistic(str, Enum):
    rank = "rank"
    percentile = "percentile"
    z_score = "z_score"
    similarity = "similarity"
    raw = "raw"


class CohortType(str, Enum):
    current_draft = "current_draft"
    all_time_draft = "all_time_draft"
    current_nba = "current_nba"
    all_time_nba = "all_time_nba"
    global_scope = "global_scope"


class MetricCategory(str, Enum):
    anthropometrics = "anthropometrics"
    combine_performance = "combine_performance"
    shooting = "shooting"
    advanced_stats = "advanced_stats"


class SimilarityDimension(str, Enum):
    anthro = "anthro"
    combine = "combine"
    shooting = "shooting"
    composite = "composite"
