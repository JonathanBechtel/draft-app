from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import or_, select
from sqlmodel import delete
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.fields import (
    CohortType,
    MetricCategory,
    MetricSource,
    MetricStatistic,
)
from app.models.position_taxonomy import (
    PositionScope,
    PositionScopeKind,
    resolve_position_scope,
)
from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.combine_shooting import CombineShootingResult
from app.schemas.metrics import MetricDefinition, MetricSnapshot, PlayerMetricValue
from app.schemas.player_status import PlayerStatus
from app.schemas.seasons import Season
from app.utils.db_async import SessionLocal, load_schema_modules


MIN_SAMPLE_DEFAULT = 3


@dataclass(frozen=True)
class MetricSpec:
    metric_key: str
    display_name: str
    source: MetricSource
    category: MetricCategory
    column: str
    unit: Optional[str] = None
    lower_is_better: bool = False
    description: Optional[str] = None
    drill: Optional[str] = None


ANTHRO_SPECS: Tuple[MetricSpec, ...] = (
    MetricSpec(
        metric_key="wingspan_in",
        display_name="Wingspan",
        source=MetricSource.combine_anthro,
        category=MetricCategory.anthropometrics,
        column="wingspan_in",
        unit="inches",
    ),
    MetricSpec(
        metric_key="standing_reach_in",
        display_name="Standing Reach",
        source=MetricSource.combine_anthro,
        category=MetricCategory.anthropometrics,
        column="standing_reach_in",
        unit="inches",
    ),
    MetricSpec(
        metric_key="height_w_shoes_in",
        display_name="Height (With Shoes)",
        source=MetricSource.combine_anthro,
        category=MetricCategory.anthropometrics,
        column="height_w_shoes_in",
        unit="inches",
    ),
    MetricSpec(
        metric_key="height_wo_shoes_in",
        display_name="Height (Without Shoes)",
        source=MetricSource.combine_anthro,
        category=MetricCategory.anthropometrics,
        column="height_wo_shoes_in",
        unit="inches",
    ),
    MetricSpec(
        metric_key="weight_lb",
        display_name="Weight",
        source=MetricSource.combine_anthro,
        category=MetricCategory.anthropometrics,
        column="weight_lb",
        unit="pounds",
    ),
    MetricSpec(
        metric_key="body_fat_pct",
        display_name="Body Fat",
        source=MetricSource.combine_anthro,
        category=MetricCategory.anthropometrics,
        column="body_fat_pct",
        unit="percent",
        lower_is_better=True,
    ),
    MetricSpec(
        metric_key="hand_length_in",
        display_name="Hand Length",
        source=MetricSource.combine_anthro,
        category=MetricCategory.anthropometrics,
        column="hand_length_in",
        unit="inches",
    ),
    MetricSpec(
        metric_key="hand_width_in",
        display_name="Hand Width",
        source=MetricSource.combine_anthro,
        category=MetricCategory.anthropometrics,
        column="hand_width_in",
        unit="inches",
    ),
)

AGILITY_SPECS: Tuple[MetricSpec, ...] = (
    MetricSpec(
        metric_key="lane_agility_time_s",
        display_name="Lane Agility Time",
        source=MetricSource.combine_agility,
        category=MetricCategory.combine_performance,
        column="lane_agility_time_s",
        unit="seconds",
        lower_is_better=True,
    ),
    MetricSpec(
        metric_key="shuttle_run_s",
        display_name="Shuttle Run",
        source=MetricSource.combine_agility,
        category=MetricCategory.combine_performance,
        column="shuttle_run_s",
        unit="seconds",
        lower_is_better=True,
    ),
    MetricSpec(
        metric_key="three_quarter_sprint_s",
        display_name="Three-Quarter Sprint",
        source=MetricSource.combine_agility,
        category=MetricCategory.combine_performance,
        column="three_quarter_sprint_s",
        unit="seconds",
        lower_is_better=True,
    ),
    MetricSpec(
        metric_key="standing_vertical_in",
        display_name="Standing Vertical",
        source=MetricSource.combine_agility,
        category=MetricCategory.combine_performance,
        column="standing_vertical_in",
        unit="inches",
    ),
    MetricSpec(
        metric_key="max_vertical_in",
        display_name="Max Vertical",
        source=MetricSource.combine_agility,
        category=MetricCategory.combine_performance,
        column="max_vertical_in",
        unit="inches",
    ),
    MetricSpec(
        metric_key="bench_press_reps",
        display_name="Bench Press Reps",
        source=MetricSource.combine_agility,
        category=MetricCategory.combine_performance,
        column="bench_press_reps",
    ),
)

SHOOTING_DRILL_LABELS = {
    "off_dribble": "Off-Dribble",
    "spot_up": "Spot-Up",
    "three_point_star": "Three-Point Star",
    "midrange_star": "Mid-Range Star",
    "three_point_side": "Corner Three",
    "midrange_side": "Corner Mid-Range",
    "free_throw": "Free Throw",
}

SHOOTING_SPECS: Tuple[MetricSpec, ...] = tuple(
    MetricSpec(
        metric_key=f"{drill}_fg_pct",
        display_name=f"{label} FG%",
        source=MetricSource.combine_shooting,
        category=MetricCategory.combine_performance,
        column="fg_pct",
        unit="percent",
        drill=drill,
    )
    for drill, label in SHOOTING_DRILL_LABELS.items()
)

ALL_SPECS: Tuple[MetricSpec, ...] = ANTHRO_SPECS + AGILITY_SPECS + SHOOTING_SPECS


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute DraftGuru metric snapshots.")
    parser.add_argument(
        "--cohort",
        required=True,
        choices=[c.value for c in CohortType],
        help="Cohort to evaluate (e.g., current_draft)",
    )
    parser.add_argument(
        "--season",
        help="Season code like 2024-25 (required for current_draft)",
    )
    parser.add_argument(
        "--position-scope",
        dest="position_scope",
        help=(
            "Limit cohort to a position (fine: pg, sg, sf, pf, c, pg-sg, etc. | "
            "parent: guard, wing, forward, big)"
        ),
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        choices=[c.value for c in MetricCategory],
        help="Metric categories to compute (defaults to all)",
    )
    parser.add_argument(
        "--run-key",
        help="Unique key for this computation run (auto-generated when omitted)",
    )
    parser.add_argument(
        "--min-sample",
        type=int,
        default=MIN_SAMPLE_DEFAULT,
        help="Minimum sample size required to emit a metric",
    )
    parser.add_argument(
        "--notes",
        help="Optional notes stored on the metric snapshot",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute metrics without writing to the database",
    )
    parser.add_argument(
        "--replace-run",
        action="store_true",
        help="Delete existing data with the same run key before inserting",
    )
    return parser.parse_args(argv)


def pick_position_scope(value: Optional[str]) -> Optional[PositionScope]:
    return resolve_position_scope(value)


def pick_categories(values: Optional[Iterable[str]]) -> Set[MetricCategory]:
    if not values:
        return set(MetricCategory)
    return {MetricCategory(v) for v in values}


async def resolve_season(session: "AsyncSession", code: str) -> Season:
    stmt = select(Season).where(Season.code == code)
    result = await session.execute(stmt)
    season = result.scalars().first()
    if season is None:
        raise ValueError(f"Season code {code!r} not found")
    return season


async def ensure_metric_definitions(
    session: "AsyncSession", specs: Sequence[MetricSpec]
) -> Dict[str, MetricDefinition]:
    keys = [spec.metric_key for spec in specs]
    result = await session.execute(
        select(MetricDefinition).where(MetricDefinition.metric_key.in_(keys))
    )
    existing = result.unique().scalars().all()
    existing_map = {row.metric_key: row for row in existing}

    for spec in specs:
        if spec.metric_key in existing_map:
            continue
        definition = MetricDefinition(
            metric_key=spec.metric_key,
            display_name=spec.display_name,
            short_label=None,
            source=spec.source,
            statistic=MetricStatistic.raw,
            category=spec.category,
            unit=spec.unit,
            description=spec.description,
        )
        session.add(definition)
        existing_map[spec.metric_key] = definition

    await session.flush()
    return existing_map


async def load_anthro(
    session: "AsyncSession", season_ids: Optional[Set[int]]
) -> pd.DataFrame:
    stmt = (
        select(
            CombineAnthro.player_id,
            CombineAnthro.season_id,
            CombineAnthro.raw_position,
            CombineAnthro.position_fine,
            CombineAnthro.position_parents,
            CombineAnthro.body_fat_pct,
            CombineAnthro.hand_length_in,
            CombineAnthro.hand_width_in,
            CombineAnthro.height_w_shoes_in,
            CombineAnthro.height_wo_shoes_in,
            CombineAnthro.standing_reach_in,
            CombineAnthro.wingspan_in,
            CombineAnthro.weight_lb,
            PlayerStatus.is_active_nba,
            PlayerStatus.nba_last_season,
        )
        .select_from(CombineAnthro)
        .join(
            PlayerStatus,
            PlayerStatus.player_id == CombineAnthro.player_id,
            isouter=True,
        )
    )
    if season_ids:
        stmt = stmt.where(CombineAnthro.season_id.in_(season_ids))
    result = await session.execute(stmt)
    rows = result.mappings().all()
    return pd.DataFrame(rows)


async def load_agility(
    session: "AsyncSession", season_ids: Optional[Set[int]]
) -> pd.DataFrame:
    stmt = (
        select(
            CombineAgility.player_id,
            CombineAgility.season_id,
            CombineAgility.raw_position,
            CombineAgility.position_fine,
            CombineAgility.position_parents,
            CombineAgility.lane_agility_time_s,
            CombineAgility.shuttle_run_s,
            CombineAgility.three_quarter_sprint_s,
            CombineAgility.standing_vertical_in,
            CombineAgility.max_vertical_in,
            CombineAgility.bench_press_reps,
            PlayerStatus.is_active_nba,
            PlayerStatus.nba_last_season,
        )
        .select_from(CombineAgility)
        .join(
            PlayerStatus,
            PlayerStatus.player_id == CombineAgility.player_id,
            isouter=True,
        )
    )
    if season_ids:
        stmt = stmt.where(CombineAgility.season_id.in_(season_ids))
    result = await session.execute(stmt)
    rows = result.mappings().all()
    return pd.DataFrame(rows)


async def load_shooting(
    session: "AsyncSession", season_ids: Optional[Set[int]]
) -> pd.DataFrame:
    stmt = (
        select(
            CombineShootingResult.player_id,
            CombineShootingResult.season_id,
            CombineShootingResult.raw_position,
            CombineShootingResult.position_fine,
            CombineShootingResult.position_parents,
            CombineShootingResult.drill,
            CombineShootingResult.fgm,
            CombineShootingResult.fga,
            PlayerStatus.is_active_nba,
            PlayerStatus.nba_last_season,
        )
        .select_from(CombineShootingResult)
        .join(
            PlayerStatus,
            PlayerStatus.player_id == CombineShootingResult.player_id,
            isouter=True,
        )
    )
    if season_ids:
        stmt = stmt.where(CombineShootingResult.season_id.in_(season_ids))
    result = await session.execute(stmt)
    rows = result.mappings().all()
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["fg_pct"] = df.apply(
        lambda row: (row["fgm"] / row["fga"]) * 100
        if row["fga"] not in (0, None)
        else pd.NA,
        axis=1,
    )
    return df


class MetricRunner:
    def __init__(self, session, args: argparse.Namespace) -> None:
        self.session = session
        self.cohort = CohortType(args.cohort)
        self.position_scope = pick_position_scope(args.position_scope)
        self.categories = pick_categories(args.categories)
        self.min_sample = max(1, args.min_sample)
        self.notes = args.notes
        self.dry_run = args.dry_run
        self.replace_run = args.replace_run
        self.season_code = args.season
        self.run_key = args.run_key or self._default_run_key(self.season_code)

        self.season: Optional[Season] = None
        self.season_ids: Optional[Set[int]] = None

        self.specs = [spec for spec in ALL_SPECS if spec.category in self.categories]
        if not self.specs:
            raise ValueError("No metric specifications selected")
        self.sources: List[MetricSource] = list(
            dict.fromkeys(spec.source for spec in self.specs)
        )

    @staticmethod
    def _default_run_key(season_code: Optional[str]) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        season_part = season_code or "all"
        return f"metrics_{season_part}_{stamp}"

    async def run(self) -> None:
        await self._configure_cohort()
        definitions = await ensure_metric_definitions(self.session, self.specs)

        raw_frames = await self._load_source_frames()
        results_by_source: Dict[MetricSource, List[Tuple[MetricSpec, pd.DataFrame]]] = {
            source: [] for source in self.sources
        }
        diagnostics: List[Dict[str, object]] = []
        players_by_source: Dict[MetricSource, Set[int]] = {
            source: set() for source in self.sources
        }
        total_players: Set[int] = set()

        for spec in self.specs:
            frame = raw_frames.get(spec.source)
            spec_frame = self._prepare_spec_frame(frame, spec)
            metrics_df, diag = self._compute_metrics(spec_frame, spec)
            diag["source"] = spec.source.value
            diagnostics.append(diag)
            if metrics_df is None:
                continue
            player_ids = metrics_df["player_id"].astype(int).tolist()
            players_by_source[spec.source].update(player_ids)
            total_players.update(player_ids)
            results_by_source[spec.source].append((spec, metrics_df))

        if not any(results_by_source.values()):
            print("No metrics produced; exiting without snapshot.")
            self._report(
                diagnostics,
                snapshots={},
                populations={source: 0 for source in self.sources},
                total_population=0,
            )
            return

        populations = {
            source: len(players_by_source[source])
            for source in self.sources
            if results_by_source[source]
        }
        snapshot_details: Dict[MetricSource, Dict[str, object]] = {}

        if not self.dry_run:
            if self.replace_run:
                await self._delete_existing_run()

            payload_buffer: List[PlayerMetricValue] = []
            for source in self.sources:
                source_results = results_by_source[source]
                if not source_results:
                    continue
                run_key = self._run_key_for_source(source)
                snapshot = MetricSnapshot(
                    run_key=run_key,
                    cohort=self.cohort,
                    season_id=self.season.id if self.season else None,
                    source=source,
                    population_size=populations.get(source, 0),
                    notes=self.notes,
                    **self._position_scope_kwargs(),
                )
                self.session.add(snapshot)
                await self.session.flush()

                payload = self._build_values(snapshot, source_results, definitions)
                if payload:
                    payload_buffer.extend(payload)
                    snapshot_details[source] = {
                        "snapshot_id": snapshot.id,
                        "run_key": run_key,
                    }
                else:
                    print(
                        f"Computed metrics yielded no rows for {source.value}; removing snapshot."
                    )
                    await self.session.delete(snapshot)

            if payload_buffer:
                self.session.add_all(payload_buffer)
                await self.session.commit()
            else:
                print(
                    "Computed metrics yielded no rows; rolling back snapshot creation."
                )
                await self.session.rollback()
                snapshot_details.clear()
        else:
            await self.session.rollback()
            for source in self.sources:
                if results_by_source[source]:
                    snapshot_details[source] = {
                        "snapshot_id": None,
                        "run_key": self._run_key_for_source(source),
                    }

        self._report(
            diagnostics,
            snapshots=snapshot_details,
            populations={source: populations.get(source, 0) for source in self.sources},
            total_population=len(total_players),
        )

    async def _configure_cohort(self) -> None:
        if self.cohort == CohortType.current_draft:
            if not self.season_code:
                raise ValueError("--season is required for current_draft cohorts")
            season = await resolve_season(self.session, self.season_code)
            season_id = season.id
            if season_id is None:
                raise ValueError(
                    f"Season {self.season_code!r} is missing a persisted identifier"
                )
            self.season = season
            self.season_ids = {season_id}
        else:
            self.season = None
            self.season_ids = None

    async def _load_source_frames(self) -> Dict[MetricSource, pd.DataFrame]:
        frames: Dict[MetricSource, pd.DataFrame] = {}
        relevant_sources = set(self.sources)
        if MetricSource.combine_anthro in relevant_sources:
            df = await load_anthro(self.session, self.season_ids)
            frames[MetricSource.combine_anthro] = self._apply_common_filters(df)
        if MetricSource.combine_agility in relevant_sources:
            df = await load_agility(self.session, self.season_ids)
            frames[MetricSource.combine_agility] = self._apply_common_filters(df)
        if MetricSource.combine_shooting in relevant_sources:
            df = await load_shooting(self.session, self.season_ids)
            frames[MetricSource.combine_shooting] = self._apply_common_filters(df)
        return frames

    def _run_key_for_source(self, source: MetricSource) -> str:
        if len(self.sources) <= 1:
            return self.run_key
        return f"{self.run_key}:{source.value}"

    def _position_scope_kwargs(self) -> Dict[str, Optional[str]]:
        if not self.position_scope:
            return {}
        if self.position_scope.kind == PositionScopeKind.fine:
            return {"position_scope_fine": self.position_scope.value}
        return {"position_scope_parent": self.position_scope.value}

    def _apply_common_filters(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        filtered = df.copy()
        if self.position_scope:
            if self.position_scope.kind == PositionScopeKind.fine:
                fine_series = filtered.get("position_fine")
                if fine_series is None:
                    return filtered.iloc[0:0]
                filtered = filtered[fine_series == self.position_scope.value]
            else:
                parents_series = filtered.get("position_parents")
                if parents_series is None:
                    return filtered.iloc[0:0]
                mask = parents_series.apply(
                    lambda parents: isinstance(parents, list)
                    and self.position_scope.value in parents
                )
                filtered = filtered[mask]
        return self._annotate_baseline(filtered)

    def _annotate_baseline(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            df["baseline_flag"] = pd.Series(dtype=bool)
            return df
        annotated = df.copy()
        default_false = pd.Series(False, index=annotated.index)
        active_series = (
            annotated.get("is_active_nba", default_false).fillna(False).astype(bool)
        )
        nba_history = annotated.get("nba_last_season")
        history_series = (
            nba_history.notna() if nba_history is not None else default_false
        )

        if self.cohort == CohortType.current_nba:
            baseline = active_series
        elif self.cohort == CohortType.all_time_nba:
            baseline = (active_series | history_series).astype(bool)
        else:
            baseline = pd.Series(True, index=annotated.index)

        annotated["baseline_flag"] = baseline.astype(bool)
        return annotated

    def _prepare_spec_frame(
        self, frame: Optional[pd.DataFrame], spec: MetricSpec
    ) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["player_id", "season_id", "value"])
        working = frame.copy()
        if spec.source == MetricSource.combine_shooting:
            working = working[working["drill"] == spec.drill]
        if spec.column not in working.columns:
            return pd.DataFrame(columns=["player_id", "season_id", "value"])
        columns = ["player_id", "season_id", spec.column]
        if "baseline_flag" in working.columns:
            columns.append("baseline_flag")
        working = working[columns].rename(columns={spec.column: "value"})
        working = working.dropna(subset=["player_id", "value"])
        if working.empty:
            return working
        # Deduplicate by player so all-time cohorts keep the most recent measurement.
        if "season_id" in working.columns:
            working = working.sort_values("season_id").drop_duplicates(
                subset=["player_id"], keep="last"
            )
        if "baseline_flag" not in working.columns:
            working["baseline_flag"] = False
        return working

    def _compute_metrics(
        self, df: pd.DataFrame, spec: MetricSpec
    ) -> Tuple[Optional[pd.DataFrame], Dict[str, object]]:
        diagnostics: Dict[str, object] = {
            "metric_key": spec.metric_key,
            "count": 0,
            "skipped": True,
        }
        if df.empty:
            diagnostics["reason"] = "no_data"
            return None, diagnostics

        df = df.copy()
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna(subset=["value"])
        if df.empty:
            diagnostics["reason"] = "no_valid_values"
            return None, diagnostics

        baseline_mask = df.get("baseline_flag")
        if baseline_mask is None:
            baseline_mask = pd.Series(False, index=df.index)
        baseline_mask = baseline_mask.fillna(False).astype(bool)
        baseline_values = df.loc[baseline_mask, "value"]
        baseline_count = int(baseline_values.count())
        diagnostics.update(
            {
                "count": baseline_count,
                "population_count": int(df["value"].count()),
                "skipped": False,
            }
        )

        if baseline_count == 0:
            diagnostics["skipped"] = True
            diagnostics["reason"] = "no_active_baseline"
            return None, diagnostics

        if baseline_count < self.min_sample:
            diagnostics["skipped"] = True
            diagnostics["reason"] = f"insufficient_sample(<{self.min_sample})"
            return None, diagnostics

        value_series = df["value"]
        sorted_baseline = np.sort(baseline_values.to_numpy())
        ascending_rank = spec.lower_is_better

        positions = np.searchsorted(
            sorted_baseline, value_series.to_numpy(), side="right"
        )
        rank_pct = positions / baseline_count
        if spec.lower_is_better:
            adjustment = 100.0 / baseline_count
            percentile_vals = ((1 - rank_pct) * 100.0) + adjustment
        else:
            percentile_vals = rank_pct * 100.0
        percentile_series = pd.Series(percentile_vals, index=df.index)

        if ascending_rank:
            unique_vals = np.sort(baseline_values.unique())
            rank_positions = np.searchsorted(
                unique_vals, value_series.to_numpy(), side="left"
            )
        else:
            unique_vals = np.sort((-baseline_values).unique())
            rank_positions = np.searchsorted(
                unique_vals, (-value_series).to_numpy(), side="left"
            )
        rank_series = pd.Series(rank_positions + 1, index=df.index)

        mean = baseline_values.mean()
        std = baseline_values.std(ddof=0)
        z_scores = pd.Series([pd.NA] * len(df), index=df.index)
        if std and std > 0:
            z = (value_series - mean) / std
            if spec.lower_is_better:
                z = -1 * z
            z_scores = z

        diagnostics.update(
            {
                "mean": float(mean),
                "std": float(std) if std and std > 0 else None,
                "min": float(baseline_values.min()),
                "max": float(baseline_values.max()),
            }
        )

        metrics_df = pd.DataFrame(
            {
                "player_id": df["player_id"].astype(int),
                "season_id": df["season_id"],
                "raw_value": value_series,
                "rank": rank_series,
                "percentile": percentile_series.clip(0, 100),
                "z_score": z_scores,
            }
        )
        return metrics_df, diagnostics

    async def _delete_existing_run(self) -> None:
        pattern = f"{self.run_key}:%"
        result = await self.session.exec(
            select(MetricSnapshot).where(
                or_(
                    MetricSnapshot.run_key == self.run_key,
                    MetricSnapshot.run_key.like(pattern),
                )
            )
        )
        snapshots = result.scalars().all()
        if not snapshots:
            return
        snapshot_ids = [
            snapshot.id for snapshot in snapshots if snapshot.id is not None
        ]
        if snapshot_ids:
            await self.session.exec(
                delete(PlayerMetricValue).where(
                    PlayerMetricValue.snapshot_id.in_(snapshot_ids)
                )
            )
        for snapshot in snapshots:
            await self.session.delete(snapshot)
        await self.session.flush()

    def _build_values(
        self,
        snapshot: MetricSnapshot,
        results: Sequence[Tuple[MetricSpec, pd.DataFrame]],
        definitions: Dict[str, MetricDefinition],
    ) -> List[PlayerMetricValue]:
        payload: List[PlayerMetricValue] = []
        for spec, df in results:
            definition = definitions[spec.metric_key]
            for row in df.itertuples(index=False):
                payload.append(
                    PlayerMetricValue(
                        snapshot_id=snapshot.id,
                        metric_definition_id=definition.id,
                        player_id=int(row.player_id),
                        raw_value=float(row.raw_value)
                        if pd.notna(row.raw_value)
                        else None,
                        rank=int(row.rank) if pd.notna(row.rank) else None,
                        percentile=float(row.percentile)
                        if pd.notna(row.percentile)
                        else None,
                        z_score=float(row.z_score) if pd.notna(row.z_score) else None,
                    )
                )
        return payload

    def _report(
        self,
        diagnostics: Sequence[Dict[str, object]],
        *,
        snapshots: Dict[MetricSource, Dict[str, object]],
        populations: Dict[MetricSource, int],
        total_population: int,
    ) -> None:
        header = f"Run key base: {self.run_key} | Total population: {total_population}"
        print(header)
        for source in self.sources:
            info = snapshots.get(source)
            default_run_key = self._run_key_for_source(source)
            run_key = (
                info.get("run_key", default_run_key)
                if info is not None
                else default_run_key
            )
            snapshot_id_val = info.get("snapshot_id") if info is not None else None
            snapshot_id = snapshot_id_val if isinstance(snapshot_id_val, int) else None
            status = (
                f"Snapshot ID: {snapshot_id}"
                if snapshot_id is not None
                else "Snapshot not persisted"
            )
            population = populations.get(source, 0)
            print(
                f" - Source {source.value}: run_key={run_key} | {status} | population={population}"
            )
        for diag in diagnostics:
            metric_key = diag.get("metric_key")
            diag_source = diag.get("source")
            prefix = f"[{diag_source}] " if diag_source else ""
            if diag.get("skipped"):
                reason = diag.get("reason", "skipped")
                print(f"   - {prefix}{metric_key}: skipped ({reason})")
            else:
                parts = [
                    f"count={diag.get('count')}",
                    f"mean={diag.get('mean'):.3f}"
                    if diag.get("mean") is not None
                    else "mean=na",
                    f"std={diag.get('std'):.3f}"
                    if diag.get("std") is not None
                    else "std=na",
                ]
                print(f"   - {prefix}{metric_key}: " + ", ".join(parts))


async def main_async(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    load_schema_modules()
    async with SessionLocal() as session:
        runner = MetricRunner(session, args)
        await runner.run()


def main(argv: Optional[Sequence[str]] = None) -> None:
    asyncio.run(main_async(argv))


if __name__ == "__main__":
    main()
