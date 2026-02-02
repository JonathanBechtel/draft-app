"""Compute derived metric snapshots for a player cohort."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, cast
from itertools import chain

import numpy as np
import pandas as pd
from sqlalchemy import select, func
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
    preset_scope_tokens,
    resolve_position_scope,
)
from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.combine_shooting import CombineShooting, SHOOTING_DRILL_COLUMNS
from app.schemas.metrics import MetricDefinition, MetricSnapshot, PlayerMetricValue
from app.schemas.player_status import PlayerStatus
from app.schemas.positions import Position
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
        category=MetricCategory.shooting,
        column="fg_pct",
        unit="percent",
        drill=drill,
    )
    for drill, label in SHOOTING_DRILL_LABELS.items()
)

ALL_SPECS: Tuple[MetricSpec, ...] = ANTHRO_SPECS + AGILITY_SPECS + SHOOTING_SPECS


@dataclass(frozen=True)
class ScopePlanEntry:
    display: str
    scope: Optional[PositionScope]
    label: Optional[str]
    append_suffix: bool


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
        help="Season code like 2024-25 (required for current_draft; use 'all' for global all-seasons)",
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
        "--position-matrix",
        choices=["parent", "fine"],
        help=(
            "Run a full sweep of position scopes (parent: guard/wing/forward/big; "
            "fine: pg/sg/sf/pf/c + common hybrids)."
        ),
    )
    parser.add_argument(
        "--matrix-skip-baseline",
        action="store_true",
        help=(
            "When set alongside --position-matrix, skip the all-positions baseline run."
        ),
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        choices=[c.value for c in MetricCategory],
        help="Metric categories to compute (defaults to all)",
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=[s.value for s in MetricSource],
        help=(
            "Limit computation to specific metric sources within the selected "
            "categories (e.g., combine_anthro, combine_agility, combine_shooting)."
        ),
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
            Position.code.label("position_fine"),
            cast(Any, Position.parents).label("position_parents"),
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
        .join(Position, Position.id == CombineAnthro.position_id, isouter=True)
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
            Position.code.label("position_fine"),
            cast(Any, Position.parents).label("position_parents"),
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
        .join(Position, Position.id == CombineAgility.position_id, isouter=True)
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
    drill_columns = list(chain.from_iterable(SHOOTING_DRILL_COLUMNS.values()))
    select_columns = [
        CombineShooting.player_id,
        CombineShooting.season_id,
        CombineShooting.raw_position,
        CombineShooting.position_id,
        Position.code.label("position_fine"),
        cast(Any, Position.parents).label("position_parents"),
        PlayerStatus.is_active_nba,
        PlayerStatus.nba_last_season,
    ] + [getattr(CombineShooting, col) for col in drill_columns]
    stmt = (
        select(*select_columns)
        .select_from(CombineShooting)
        .join(Position, Position.id == CombineShooting.position_id, isouter=True)
        .join(
            PlayerStatus,
            PlayerStatus.player_id == CombineShooting.player_id,
            isouter=True,
        )
    )
    if season_ids:
        stmt = stmt.where(CombineShooting.season_id.in_(season_ids))
    result = await session.execute(stmt)
    rows = result.mappings().all()
    wide_df = pd.DataFrame(rows)
    if wide_df.empty:
        return wide_df

    records: List[Dict[str, object]] = []
    for _, row in wide_df.iterrows():
        base = {
            "player_id": row["player_id"],
            "season_id": row["season_id"],
            "raw_position": row.get("raw_position"),
            "position_fine": row.get("position_fine"),
            "position_parents": row.get("position_parents"),
            "is_active_nba": row.get("is_active_nba"),
            "nba_last_season": row.get("nba_last_season"),
        }
        for drill, (fgm_col, fga_col) in SHOOTING_DRILL_COLUMNS.items():
            fgm = row.get(fgm_col)
            fga = row.get(fga_col)
            if pd.isna(fgm):
                fgm = None
            if pd.isna(fga):
                fga = None
            if fgm is None and fga is None:
                continue
            entry = dict(base)
            entry.update({"drill": drill, "fgm": fgm, "fga": fga})
            records.append(entry)

    df = pd.DataFrame(records)
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
        self.position_matrix = args.position_matrix
        self.matrix_skip_baseline = args.matrix_skip_baseline
        self.categories = pick_categories(args.categories)
        self._source_filters: Optional[Set[MetricSource]] = (
            {MetricSource(v) for v in args.sources}
            if getattr(args, "sources", None)
            else None
        )
        self.min_sample = max(1, args.min_sample)
        self.notes = args.notes
        self.dry_run = args.dry_run
        self.replace_run = args.replace_run
        self.season_code = args.season
        self.base_run_key = args.run_key or self._default_run_key()

        self.season: Optional[Season] = None
        self.season_ids: Optional[Set[int]] = None

        self.specs = [spec for spec in ALL_SPECS if spec.category in self.categories]
        if self._source_filters is not None:
            self.specs = [
                spec for spec in self.specs if spec.source in self._source_filters
            ]
        if not self.specs:
            raise ValueError("No metric specifications selected")
        self.sources: List[MetricSource] = list(
            dict.fromkeys(spec.source for spec in self.specs)
        )
        self.scope_plan = self._build_scope_plan()

    def _default_run_key(self) -> str:
        if self.cohort == CohortType.global_scope:
            season_part = (self.season_code or "all").replace(" ", "_")
            return f"metrics_global_{season_part}"
        season_part = self.season_code or "all"
        cohort_part = self.cohort.value
        return f"cohort={cohort_part}|season={season_part}"

    def _compose_scope_run_key(self, entry: ScopePlanEntry) -> str:
        token = entry.label if entry.label else "all"
        return f"{self.base_run_key}|pos={token}|min={self.min_sample}"

    def _build_scope_plan(self) -> List[ScopePlanEntry]:
        if self.position_matrix and self.position_scope:
            raise ValueError(
                "--position-scope cannot be combined with --position-matrix"
            )
        plan: List[ScopePlanEntry] = []
        if self.position_matrix:
            tokens = preset_scope_tokens(self.position_matrix)
            if not self.matrix_skip_baseline:
                plan.append(
                    ScopePlanEntry(
                        display="all",
                        scope=None,
                        label=None,
                        append_suffix=False,
                    )
                )
            for token in tokens:
                scope = resolve_position_scope(token)
                if scope is None:
                    continue
                plan.append(
                    ScopePlanEntry(
                        display=token,
                        scope=scope,
                        label=token,
                        append_suffix=True,
                    )
                )
            return plan

        display = self.position_scope.value if self.position_scope else "all"
        plan.append(
            ScopePlanEntry(
                display=display,
                scope=self.position_scope,
                label=self.position_scope.value if self.position_scope else None,
                append_suffix=False,
            )
        )
        return plan

    async def run(self) -> None:
        await self._configure_cohort()
        definitions = await ensure_metric_definitions(self.session, self.specs)
        any_scope_ran = False
        for entry in self.scope_plan:
            scope_run_key = self._compose_scope_run_key(entry)
            self.position_scope = entry.scope
            print(f"\n=== Position scope: {entry.display} ===")
            scope_success = await self._execute_scope(
                definitions=definitions,
                run_key_base=scope_run_key,
                scope_label=entry.display,
            )
            any_scope_ran = any_scope_ran or scope_success
        if not any_scope_ran:
            print("No metrics produced for any requested position scope.")

    async def _execute_scope(
        self,
        *,
        definitions: Dict[str, MetricDefinition],
        run_key_base: str,
        scope_label: str,
    ) -> bool:
        raw_frames = await self._load_source_frames()
        results_by_source: Dict[MetricSource, List[Tuple[MetricSpec, pd.DataFrame]]] = {
            source: [] for source in self.sources
        }
        diagnostics: List[Dict[str, object]] = []
        baseline_players_by_source: Dict[MetricSource, Set[int]] = {
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
            baseline_flag = (
                spec_frame["baseline_flag"]
                if "baseline_flag" in spec_frame.columns
                else pd.Series(False, index=spec_frame.index)
            )
            baseline_ids = (
                spec_frame.loc[baseline_flag.fillna(False).astype(bool), "player_id"]
                .dropna()
                .astype(int)
                .tolist()
            )
            baseline_players_by_source[spec.source].update(baseline_ids)
            total_players.update(player_ids)
            results_by_source[spec.source].append((spec, metrics_df))

        if not any(results_by_source.values()):
            print("No metrics produced; skipping snapshot creation for this scope.")
            self._report(
                diagnostics,
                snapshots={},
                populations={source: 0 for source in self.sources},
                total_population=0,
                run_key_base=run_key_base,
                scope_label=scope_label,
            )
            return False

        populations = {
            source: len(baseline_players_by_source[source])
            for source in self.sources
            if results_by_source[source]
        }
        snapshot_details: Dict[MetricSource, Dict[str, object]] = {}

        if not self.dry_run:
            if self.replace_run:
                run_keys_to_delete: List[str] = [
                    self._run_key_for_source(source, run_key_base)
                    for source in self.sources
                ]
                await self._delete_existing_runs(run_keys_to_delete)

            payload_buffer: List[PlayerMetricValue] = []
            for source in self.sources:
                source_results = results_by_source[source]
                if not source_results:
                    continue
                run_key = self._run_key_for_source(source, run_key_base)
                version = await self._next_version(source, run_key)
                snapshot = MetricSnapshot(
                    run_key=run_key,
                    cohort=self.cohort,
                    season_id=self.season.id if self.season else None,
                    source=source,
                    population_size=populations.get(source, 0),
                    notes=self.notes,
                    version=version,
                    is_current=False,
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
                        "run_key": self._run_key_for_source(source, run_key_base),
                    }

        self._report(
            diagnostics,
            snapshots=snapshot_details,
            populations={source: populations.get(source, 0) for source in self.sources},
            total_population=len(total_players),
            run_key_base=run_key_base,
            scope_label=scope_label,
        )
        return True

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
        elif self.cohort == CohortType.global_scope:
            if not self.season_code:
                raise ValueError(
                    "--season is required for global cohorts (use 'all' for all seasons)"
                )
            if self.season_code.lower() == "all":
                # All seasons: no season filter
                self.season = None
                self.season_ids = None
            else:
                season = await resolve_season(self.session, self.season_code)
                if season.id is None:
                    raise ValueError(
                        f"Season {self.season_code!r} is missing a persisted identifier"
                    )
                self.season = season
                # Global but season-scoped
                self.season_ids = {season.id}
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

    def _run_key_for_source(self, source: MetricSource, run_key_base: str) -> str:
        if self.cohort == CohortType.global_scope:
            # Explicitly encode source for global runs to aid downstream selection
            return f"{run_key_base}_{source.value}"
        # Keep a shared run_key across sources; source is captured in the snapshot's column
        return run_key_base

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
                scope_val = self.position_scope.value
                mask = parents_series.apply(
                    lambda parents: isinstance(parents, list) and scope_val in parents
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

        if spec.lower_is_better:
            # Competition rank: 1 + number of baseline values strictly lower.
            rank_series = pd.Series(
                np.searchsorted(sorted_baseline, value_series.to_numpy(), side="left")
                + 1,
                index=df.index,
            )
        else:
            # Competition rank: 1 + number of baseline values strictly higher.
            pos_right = np.searchsorted(
                sorted_baseline, value_series.to_numpy(), side="right"
            )
            rank_series = pd.Series((baseline_count - pos_right) + 1, index=df.index)

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
                "population_size": pd.Series(
                    baseline_count, index=df.index, dtype="int64"
                ),
            }
        )
        return metrics_df, diagnostics

    async def _delete_existing_runs(self, run_keys: Sequence[str]) -> None:
        run_keys_unique = list(dict.fromkeys(run_keys))
        if not run_keys_unique:
            return
        result = await self.session.execute(
            select(MetricSnapshot).where(
                MetricSnapshot.run_key.in_(run_keys_unique),
                MetricSnapshot.cohort == self.cohort,  # type: ignore[arg-type]
            )
        )
        snapshots = result.scalars().all()
        if not snapshots:
            return
        snapshot_ids = [
            snapshot.id for snapshot in snapshots if snapshot.id is not None
        ]
        if snapshot_ids:
            await self.session.execute(
                delete(PlayerMetricValue).where(
                    PlayerMetricValue.snapshot_id.in_(snapshot_ids)
                )
            )
        for snapshot in snapshots:
            await self.session.delete(snapshot)
        await self.session.flush()

    async def _next_version(self, source: MetricSource, run_key: str) -> int:
        result = await self.session.execute(
            select(func.max(MetricSnapshot.version)).where(
                MetricSnapshot.source == source,  # type: ignore[arg-type]
                MetricSnapshot.run_key == run_key,
                MetricSnapshot.cohort == self.cohort,  # type: ignore[arg-type]
            )
        )
        max_ver = result.scalar()
        return int(max_ver or 0) + 1

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
                        extra_context={
                            "population_size": int(row.population_size)
                            if pd.notna(row.population_size)
                            else None
                        },
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
        run_key_base: str,
        scope_label: str,
    ) -> None:
        header = (
            f"Run key base: {run_key_base} | Scope: {scope_label} | "
            f"Total population: {total_population}"
        )
        print(header)
        for source in self.sources:
            info = snapshots.get(source)
            default_run_key = self._run_key_for_source(source, run_key_base)
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
