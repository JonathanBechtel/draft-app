from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd
from sqlalchemy import select
from sqlmodel import delete
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.fields import (
    CohortType,
    MetricCategory,
    MetricSource,
    MetricStatistic,
    Position,
)
from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.combine_shooting import CombineShootingResult
from app.schemas.metrics import MetricDefinition, MetricSnapshot, PlayerMetricValue
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
        help="Limit cohort to a position (g, f, c, guard, forward, center)",
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


def pick_position(value: Optional[str]) -> Optional[Position]:
    if value is None:
        return None
    candidate = value.strip().lower()
    for position in Position:
        if candidate in {
            position.name,
            position.value,
            position.name[0],
            position.value[0],
        }:
            return position
    raise ValueError(f"Unknown position scope: {value}")


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
        select(MetricDefinition).where(
            MetricDefinition.__table__.c.metric_key.in_(keys)
        )
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
    stmt = select(
        CombineAnthro.player_id,
        CombineAnthro.season_id,
        CombineAnthro.pos,
        CombineAnthro.body_fat_pct,
        CombineAnthro.hand_length_in,
        CombineAnthro.hand_width_in,
        CombineAnthro.height_w_shoes_in,
        CombineAnthro.height_wo_shoes_in,
        CombineAnthro.standing_reach_in,
        CombineAnthro.wingspan_in,
        CombineAnthro.weight_lb,
    )
    if season_ids:
        stmt = stmt.where(CombineAnthro.__table__.c.season_id.in_(season_ids))
    result = await session.execute(stmt)
    rows = result.mappings().all()
    return pd.DataFrame(rows)


async def load_agility(
    session: "AsyncSession", season_ids: Optional[Set[int]]
) -> pd.DataFrame:
    stmt = select(
        CombineAgility.player_id,
        CombineAgility.season_id,
        CombineAgility.pos,
        CombineAgility.lane_agility_time_s,
        CombineAgility.shuttle_run_s,
        CombineAgility.three_quarter_sprint_s,
        CombineAgility.standing_vertical_in,
        CombineAgility.max_vertical_in,
        CombineAgility.bench_press_reps,
    )
    if season_ids:
        stmt = stmt.where(CombineAgility.__table__.c.season_id.in_(season_ids))
    result = await session.execute(stmt)
    rows = result.mappings().all()
    return pd.DataFrame(rows)


async def load_shooting(
    session: "AsyncSession", season_ids: Optional[Set[int]]
) -> pd.DataFrame:
    stmt = select(
        CombineShootingResult.player_id,
        CombineShootingResult.season_id,
        CombineShootingResult.pos,
        CombineShootingResult.drill,
        CombineShootingResult.fgm,
        CombineShootingResult.fga,
    )
    if season_ids:
        stmt = stmt.where(CombineShootingResult.__table__.c.season_id.in_(season_ids))
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
        self.position_scope = pick_position(args.position_scope)
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

    @staticmethod
    def _default_run_key(season_code: Optional[str]) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        season_part = season_code or "all"
        return f"metrics_{season_part}_{stamp}"

    async def run(self) -> None:
        await self._configure_cohort()
        definitions = await ensure_metric_definitions(self.session, self.specs)

        raw_frames = await self._load_source_frames()
        results: List[Tuple[MetricSpec, pd.DataFrame]] = []
        diagnostics: List[Dict[str, object]] = []
        players_seen: Set[int] = set()

        for spec in self.specs:
            frame = raw_frames.get(spec.source)
            spec_frame = self._prepare_spec_frame(frame, spec)
            metrics_df, diag = self._compute_metrics(spec_frame, spec)
            diagnostics.append(diag)
            if metrics_df is None:
                continue
            players_seen.update(metrics_df["player_id"].astype(int).tolist())
            results.append((spec, metrics_df))

        if not results:
            print("No metrics produced; exiting without snapshot.")
            self._report(diagnostics, snapshot_id=None, population_size=0)
            return

        population_size = len(players_seen)

        snapshot: Optional[MetricSnapshot] = None
        if not self.dry_run:
            if self.replace_run:
                await self._delete_existing_run()
            snapshot = MetricSnapshot(
                run_key=self.run_key,
                cohort=self.cohort,
                season_id=self.season.id if self.season else None,
                position_scope=self.position_scope,
                source=MetricSource.advanced_stats,
                population_size=population_size,
                notes=self.notes,
            )
            self.session.add(snapshot)
            await self.session.flush()

            payload = self._build_values(snapshot, results, definitions)
            if payload:
                self.session.add_all(payload)
                await self.session.commit()
            else:
                print(
                    "Computed metrics yielded no rows; rolling back snapshot creation."
                )
                await self.session.delete(snapshot)
                await self.session.rollback()
                snapshot = None
        else:
            await self.session.rollback()

        self._report(
            diagnostics,
            snapshot_id=snapshot.id if snapshot else None,
            population_size=population_size,
        )

    async def _configure_cohort(self) -> None:
        if self.cohort == CohortType.current_draft:
            if not self.season_code:
                raise ValueError("--season is required for current_draft cohorts")
            season = await resolve_season(self.session, self.season_code)
            if season.id is None:
                raise ValueError(
                    f"Season {self.season_code!r} is missing a persisted identifier"
                )
            self.season = season
            self.season_ids = {season.id}
        else:
            self.season = None
            self.season_ids = None

    async def _load_source_frames(self) -> Dict[MetricSource, pd.DataFrame]:
        frames: Dict[MetricSource, pd.DataFrame] = {}
        relevant_sources = {spec.source for spec in self.specs}
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

    def _apply_common_filters(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        filtered = df
        if self.position_scope:
            desired = self.position_scope.name.lower()
            pos_series = filtered.get("pos")
            if pos_series is not None:
                normalized = pos_series.fillna("").astype(str).str.lower()
                filtered = filtered[normalized.str.startswith(desired)]
        return filtered

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
        working = working[["player_id", "season_id", spec.column]].rename(
            columns={spec.column: "value"}
        )
        working = working.dropna(subset=["player_id", "value"])
        if working.empty:
            return working
        # Deduplicate by player so all-time cohorts keep the most recent measurement.
        if "season_id" in working.columns:
            working = working.sort_values("season_id").drop_duplicates(
                subset=["player_id"], keep="last"
            )
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

        sample_count = int(df["value"].count())
        diagnostics.update(
            {
                "count": sample_count,
                "skipped": False,
            }
        )

        if sample_count < self.min_sample:
            diagnostics["skipped"] = True
            diagnostics["reason"] = f"insufficient_sample(<{self.min_sample})"
            return None, diagnostics

        ascending_rank = spec.lower_is_better
        rank_series = df["value"].rank(method="dense", ascending=ascending_rank)
        rank_pct = df["value"].rank(method="average", ascending=True, pct=True)
        if spec.lower_is_better:
            adjustment = 100.0 / sample_count
            percentile_series = ((1 - rank_pct) * 100.0) + adjustment
        else:
            percentile_series = rank_pct * 100.0

        mean = df["value"].mean()
        std = df["value"].std(ddof=0)
        z_scores = pd.Series([pd.NA] * len(df), index=df.index)
        if std and std > 0:
            z = (df["value"] - mean) / std
            if spec.lower_is_better:
                z = -1 * z
            z_scores = z

        diagnostics.update(
            {
                "mean": float(mean),
                "std": float(std) if std and std > 0 else None,
                "min": float(df["value"].min()),
                "max": float(df["value"].max()),
            }
        )

        metrics_df = pd.DataFrame(
            {
                "player_id": df["player_id"].astype(int),
                "season_id": df["season_id"],
                "raw_value": df["value"],
                "rank": rank_series,
                "percentile": percentile_series.clip(0, 100),
                "z_score": z_scores,
            }
        )
        return metrics_df, diagnostics

    async def _delete_existing_run(self) -> None:
        result = await self.session.exec(
            select(MetricSnapshot).where(MetricSnapshot.run_key == self.run_key)
        )
        existing = result.first()
        if not existing:
            return
        await self.session.exec(
            delete(PlayerMetricValue).where(
                PlayerMetricValue.snapshot_id == existing.id
            )
        )
        await self.session.delete(existing)
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
        snapshot_id: Optional[int],
        population_size: int,
    ) -> None:
        header = f"Run key: {self.run_key}"
        if snapshot_id:
            header += f" | Snapshot ID: {snapshot_id}"
        else:
            header += " | Snapshot not persisted"
        header += f" | Population size: {population_size}"
        print(header)
        for diag in diagnostics:
            metric_key = diag.get("metric_key")
            if diag.get("skipped"):
                reason = diag.get("reason", "skipped")
                print(f" - {metric_key}: skipped ({reason})")
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
                print(f" - {metric_key}: " + ", ".join(parts))


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
