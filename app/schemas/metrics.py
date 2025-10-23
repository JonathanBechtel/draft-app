from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Column, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import Enum as SAEnum
from sqlmodel import Field, SQLModel

from app.models.fields import (
    CohortType,
    MetricCategory,
    MetricSource,
    MetricStatistic,
    Position,
)


class MetricDefinition(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "metric_definitions"
    __table_args__ = (UniqueConstraint("metric_key", name="uq_metric_definitions_key"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    metric_key: str = Field(
        index=True, description="Machine key like 'wingspan_percentile'"
    )
    display_name: str = Field(description="Label shown in the UI")
    short_label: Optional[str] = Field(
        default=None, description="Abbreviated label for compact displays"
    )
    source: MetricSource = Field(
        sa_column=Column(
            SAEnum(MetricSource, name="metric_source_enum"),
            nullable=False,
        )
    )
    statistic: MetricStatistic = Field(
        sa_column=Column(
            SAEnum(MetricStatistic, name="metric_statistic_enum"),
            nullable=False,
        )
    )
    category: MetricCategory = Field(
        sa_column=Column(
            SAEnum(MetricCategory, name="metric_category_enum"),
            nullable=False,
        ),
        description="Grouping used for UI tabs",
    )
    unit: Optional[str] = Field(
        default=None, description="Measurement unit, e.g., 'inches'"
    )
    description: Optional[str] = Field(
        default=None, description="Optional longer description"
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class MetricSnapshot(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "metric_snapshots"
    __table_args__ = (UniqueConstraint("run_key", name="uq_metric_snapshots_run"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    run_key: str = Field(
        index=True,
        description="Identifier for the batch run, e.g., '2024_pre_draft_v1'",
    )
    cohort: CohortType = Field(
        sa_column=Column(
            SAEnum(CohortType, name="cohort_type_enum"),
            nullable=False,
        )
    )
    season_id: Optional[int] = Field(default=None, foreign_key="seasons.id", index=True)
    position_scope: Optional[Position] = Field(
        default=None,
        sa_column=Column(
            SAEnum(Position, name="metric_position_enum"),
            nullable=True,
        ),
        description="Restrict cohort calculations to a single position",
    )
    source: MetricSource = Field(
        sa_column=Column(
            SAEnum(MetricSource, name="snapshot_source_enum"),
            nullable=False,
        )
    )
    population_size: int = Field(
        description="Number of players included in this cohort"
    )
    calculated_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = Field(default=None)
    notes: Optional[str] = Field(
        default=None, description="Optional commentary about the run"
    )


class PlayerMetricValue(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "player_metric_values"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "metric_definition_id",
            "player_id",
            name="uq_player_metric_values_snapshot_metric_player",
        ),
        Index(
            "ix_player_metric_values_player_snapshot",
            "player_id",
            "snapshot_id",
        ),
        Index(
            "ix_player_metric_values_metric_snapshot",
            "metric_definition_id",
            "snapshot_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    snapshot_id: int = Field(foreign_key="metric_snapshots.id", index=True)
    metric_definition_id: int = Field(foreign_key="metric_definitions.id", index=True)
    player_id: int = Field(foreign_key="players_master.id", index=True)
    raw_value: Optional[float] = Field(default=None)
    rank: Optional[int] = Field(default=None)
    percentile: Optional[float] = Field(default=None)
    z_score: Optional[float] = Field(default=None)
    value_bucket: Optional[str] = Field(
        default=None, description="Display-ready tier like 'elite' or 'average'"
    )
    extra_context: Optional[dict[str, Any]] = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Structured details supporting the metric",
    )
    calculated_at: datetime = Field(default_factory=datetime.utcnow)


class PlayerSimilarity(SQLModel, table=True):  # type: ignore[call-arg]
    __tablename__ = "player_similarity"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "anchor_player_id",
            "comparison_player_id",
            "category",
            name="uq_player_similarity_anchor_comp_cat",
        ),
        Index(
            "ix_player_similarity_anchor_snapshot",
            "anchor_player_id",
            "snapshot_id",
        ),
        Index(
            "ix_player_similarity_category_snapshot",
            "category",
            "snapshot_id",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    snapshot_id: int = Field(foreign_key="metric_snapshots.id", index=True)
    category: MetricCategory = Field(
        sa_column=Column(
            SAEnum(MetricCategory, name="similarity_category_enum"),
            nullable=False,
        ),
    )
    anchor_player_id: int = Field(foreign_key="players_master.id", index=True)
    comparison_player_id: int = Field(foreign_key="players_master.id", index=True)
    similarity_score: float = Field(
        description="Similarity value (0-100 or unit interval)"
    )
    rank_within_anchor: Optional[int] = Field(
        default=None, description="Ordering within the anchor's neighbour list"
    )
    shared_position: Optional[bool] = Field(default=None)
    feature_vector_version: Optional[str] = Field(
        default=None, description="Identifier for the feature set used"
    )
    details: Optional[dict[str, Any]] = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Optional per-comparison breakdown",
    )
    calculated_at: datetime = Field(default_factory=datetime.utcnow)
