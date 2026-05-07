"""Service layer for querying composite Combine Scores."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, cast

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import CohortType, MetricSource
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.metrics import MetricDefinition, MetricSnapshot, PlayerMetricValue
from app.schemas.players_master import PlayerMaster
from app.schemas.positions import Position
from app.schemas.seasons import Season


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class CombineScoreEntry:
    """A single combine score for one player and one metric key."""

    player_id: int
    metric_key: str
    display_name: str
    raw_value: float  # mean z-score
    rank: Optional[int]
    percentile: Optional[float]
    extra_context: Optional[Dict[str, Any]]


@dataclass
class PlayerCombineScores:
    """All combine scores for a single player in a given scope."""

    player_id: int
    player_name: Optional[str]
    player_slug: Optional[str]
    school: Optional[str]
    category_scores: Dict[str, CombineScoreEntry]  # keyed by metric_key
    overall_score: Optional[CombineScoreEntry]


@dataclass
class YearCombineSummary:
    """Aggregate combine score stats for a draft year."""

    season_code: str
    player_count: int
    avg_overall: Optional[float]
    median_overall: Optional[float]
    best: Optional[CombineScoreEntry]
    worst: Optional[CombineScoreEntry]
    category_averages: Dict[str, Optional[float]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COMBINE_SCORE_KEYS = {
    "combine_score_anthropometrics",
    "combine_score_athletic",
    "combine_score_shooting",
    "combine_score_overall",
}


async def _find_combine_snapshot(
    db: AsyncSession,
    cohort: CohortType,
    season_id: Optional[int],
    position_scope_parent: Optional[str] = None,
) -> Optional[int]:
    """Find the current combine_score snapshot for the given scope."""
    filters: list[Any] = [
        cast(Any, MetricSnapshot.source) == MetricSource.combine_score,
        cast(Any, MetricSnapshot.cohort) == cohort,
        cast(Any, MetricSnapshot.is_current).is_(True),
    ]
    if season_id is not None:
        filters.append(cast(Any, MetricSnapshot.season_id) == season_id)
    else:
        filters.append(cast(Any, MetricSnapshot.season_id).is_(None))
    if position_scope_parent is not None:
        filters.append(
            cast(Any, MetricSnapshot.position_scope_parent) == position_scope_parent
        )
    else:
        filters.append(cast(Any, MetricSnapshot.position_scope_parent).is_(None))
    filters.append(cast(Any, MetricSnapshot.position_scope_fine).is_(None))

    result = await db.execute(
        select(MetricSnapshot.id).where(*filters).limit(1)  # type: ignore[call-overload]
    )
    return result.scalar_one_or_none()


async def _load_score_definitions(
    db: AsyncSession,
) -> Dict[int, MetricDefinition]:
    """Load combine-score MetricDefinitions, keyed by id."""
    result = await db.execute(
        select(MetricDefinition).where(
            MetricDefinition.metric_key.in_(COMBINE_SCORE_KEYS)  # type: ignore[attr-defined]
        )
    )
    defs = result.unique().scalars().all()
    return {d.id: d for d in defs if d.id is not None}


def grade_label(percentile: Optional[float]) -> str:
    """Map a percentile to a display-friendly grade label."""
    if percentile is None:
        return "N/A"
    if percentile >= 90:
        return "Elite"
    if percentile >= 75:
        return "Above Average"
    if percentile >= 40:
        return "Average"
    if percentile >= 20:
        return "Below Average"
    return "Poor"


def grade_letter(percentile: Optional[float]) -> Optional[str]:
    """Map a percentile to a compact letter grade for UI pills (e.g. 'A-', 'B+').

    Returns None when no percentile is available so the caller can hide the
    pill rather than render a placeholder.
    """
    if percentile is None:
        return None
    if percentile >= 95:
        return "A+"
    if percentile >= 85:
        return "A"
    if percentile >= 75:
        return "A-"
    if percentile >= 65:
        return "B+"
    if percentile >= 50:
        return "B"
    if percentile >= 35:
        return "B-"
    if percentile >= 20:
        return "C"
    return "D"


# ---------------------------------------------------------------------------
# Public query functions
# ---------------------------------------------------------------------------


async def get_player_combine_scores(
    db: AsyncSession,
    player_id: int,
    cohort: CohortType = CohortType.current_draft,
    season_id: Optional[int] = None,
    position_scope_parent: Optional[str] = None,
) -> Optional[PlayerCombineScores]:
    """Fetch a player's combine scores for a given scope.

    Args:
        db: Async database session.
        player_id: The player's ID.
        cohort: Which cohort the scores were computed against.
        season_id: Season ID (required for current_draft).
        position_scope_parent: Position group scope, or None for all-positions.

    Returns:
        PlayerCombineScores if scores exist, else None.
    """
    snapshot_id = await _find_combine_snapshot(
        db, cohort, season_id, position_scope_parent
    )
    if snapshot_id is None:
        return None

    defs = await _load_score_definitions(db)
    def_ids = list(defs.keys())
    if not def_ids:
        return None

    result = await db.execute(
        select(PlayerMetricValue).where(
            PlayerMetricValue.snapshot_id == snapshot_id,  # type: ignore[arg-type]
            PlayerMetricValue.player_id == player_id,  # type: ignore[arg-type]
            PlayerMetricValue.metric_definition_id.in_(def_ids),  # type: ignore[attr-defined]
        )
    )
    rows = result.scalars().all()
    if not rows:
        return None

    # Look up player name/slug/school
    player_result = await db.execute(
        select(PlayerMaster.display_name, PlayerMaster.slug, PlayerMaster.school).where(  # type: ignore[call-overload]
            PlayerMaster.id == player_id  # type: ignore[arg-type]
        )
    )
    player_row = player_result.mappings().first()
    player_name = str(player_row["display_name"]) if player_row else None
    player_slug = str(player_row["slug"]) if player_row else None
    player_school = (
        str(player_row["school"]) if player_row and player_row["school"] else None
    )

    category_scores: Dict[str, CombineScoreEntry] = {}
    overall_score: Optional[CombineScoreEntry] = None

    for pmv in rows:
        defn = defs.get(pmv.metric_definition_id)
        if defn is None:
            continue
        entry = CombineScoreEntry(
            player_id=player_id,
            metric_key=defn.metric_key,
            display_name=defn.display_name,
            raw_value=float(pmv.raw_value) if pmv.raw_value is not None else 0.0,
            rank=int(pmv.rank) if pmv.rank is not None else None,
            percentile=float(pmv.percentile) if pmv.percentile is not None else None,
            extra_context=pmv.extra_context,
        )
        if defn.metric_key == "combine_score_overall":
            overall_score = entry
        else:
            category_scores[defn.metric_key] = entry

    return PlayerCombineScores(
        player_id=player_id,
        player_name=player_name,
        player_slug=player_slug,
        school=player_school,
        category_scores=category_scores,
        overall_score=overall_score,
    )


async def get_year_combine_scores(
    db: AsyncSession,
    season_id: int,
    cohort: CohortType = CohortType.current_draft,
    position_scope_parent: Optional[str] = None,
) -> List[PlayerCombineScores]:
    """Fetch combine scores for all players in a draft year.

    Args:
        db: Async database session.
        season_id: The season ID for the draft year.
        cohort: Cohort type.
        position_scope_parent: Position group scope, or None for all-positions.

    Returns:
        List of PlayerCombineScores, sorted by overall percentile descending.
    """
    snapshot_id = await _find_combine_snapshot(
        db, cohort, season_id, position_scope_parent
    )
    if snapshot_id is None:
        return []

    defs = await _load_score_definitions(db)
    def_ids = list(defs.keys())
    if not def_ids:
        return []

    result = await db.execute(
        select(PlayerMetricValue)
        .where(
            PlayerMetricValue.snapshot_id == snapshot_id,  # type: ignore[arg-type]
            PlayerMetricValue.metric_definition_id.in_(def_ids),  # type: ignore[attr-defined]
        )
        .order_by(PlayerMetricValue.player_id)  # type: ignore[arg-type]
    )
    rows = result.scalars().all()
    if not rows:
        return []

    # Group by player
    player_ids = list({pmv.player_id for pmv in rows})
    player_result = await db.execute(
        select(
            PlayerMaster.id,
            PlayerMaster.display_name,
            PlayerMaster.slug,
            PlayerMaster.school,  # type: ignore[call-overload]
        ).where(PlayerMaster.id.in_(player_ids))  # type: ignore[union-attr,attr-defined]
    )
    player_map = {
        r["id"]: (
            str(r["display_name"]),
            str(r["slug"]),
            str(r["school"]) if r["school"] else None,
        )
        for r in player_result.mappings().all()
    }

    by_player: Dict[int, List[PlayerMetricValue]] = {}
    for pmv in rows:
        by_player.setdefault(pmv.player_id, []).append(pmv)

    results: List[PlayerCombineScores] = []
    for pid, pmvs in by_player.items():
        name, slug, school = player_map.get(pid, (None, None, None))
        category_scores: Dict[str, CombineScoreEntry] = {}
        overall: Optional[CombineScoreEntry] = None

        for pmv in pmvs:
            defn = defs.get(pmv.metric_definition_id)
            if defn is None:
                continue
            entry = CombineScoreEntry(
                player_id=pid,
                metric_key=defn.metric_key,
                display_name=defn.display_name,
                raw_value=float(pmv.raw_value) if pmv.raw_value is not None else 0.0,
                rank=int(pmv.rank) if pmv.rank is not None else None,
                percentile=float(pmv.percentile)
                if pmv.percentile is not None
                else None,
                extra_context=pmv.extra_context,
            )
            if defn.metric_key == "combine_score_overall":
                overall = entry
            else:
                category_scores[defn.metric_key] = entry

        results.append(
            PlayerCombineScores(
                player_id=pid,
                player_name=name,
                player_slug=slug,
                school=school,
                category_scores=category_scores,
                overall_score=overall,
            )
        )

    # Sort by overall percentile descending (players without overall go last)
    results.sort(
        key=lambda p: (
            p.overall_score.percentile
            if p.overall_score and p.overall_score.percentile is not None
            else -1
        ),
        reverse=True,
    )
    return results


async def get_year_summary(
    db: AsyncSession,
    season_id: int,
    cohort: CohortType = CohortType.current_draft,
    position_scope_parent: Optional[str] = None,
) -> Optional[YearCombineSummary]:
    """Compute aggregate stats for a draft year's combine scores.

    Args:
        db: Async database session.
        season_id: The season ID.
        cohort: Cohort type.
        position_scope_parent: Position group scope.

    Returns:
        YearCombineSummary or None if no data.
    """
    players = await get_year_combine_scores(
        db, season_id, cohort, position_scope_parent
    )
    if not players:
        return None

    # Resolve season code
    season_result = await db.execute(
        select(Season.code).where(Season.id == season_id)  # type: ignore[call-overload,arg-type]
    )
    season_code = season_result.scalar_one_or_none() or str(season_id)

    # Overall percentiles
    overall_pctls = [
        p.overall_score.percentile
        for p in players
        if p.overall_score and p.overall_score.percentile is not None
    ]

    avg_overall: Optional[float] = None
    median_overall: Optional[float] = None
    best: Optional[CombineScoreEntry] = None
    worst: Optional[CombineScoreEntry] = None

    if overall_pctls:
        avg_overall = round(sum(overall_pctls) / len(overall_pctls), 1)
        sorted_pctls = sorted(overall_pctls)
        n = len(sorted_pctls)
        median_overall = round(
            sorted_pctls[n // 2]
            if n % 2
            else (sorted_pctls[n // 2 - 1] + sorted_pctls[n // 2]) / 2,
            1,
        )
        # Best and worst by overall score
        players_with_overall = [p for p in players if p.overall_score is not None]
        if players_with_overall:
            best = players_with_overall[0].overall_score  # already sorted desc
            worst = players_with_overall[-1].overall_score

    # Category averages
    category_keys = [
        "combine_score_anthropometrics",
        "combine_score_athletic",
        "combine_score_shooting",
    ]
    category_averages: Dict[str, Optional[float]] = {}
    for ck in category_keys:
        pctls: list[float] = []
        for p in players:
            if ck in p.category_scores:
                val = p.category_scores[ck].percentile
                if val is not None:
                    pctls.append(val)
        category_averages[ck] = round(sum(pctls) / len(pctls), 1) if pctls else None

    return YearCombineSummary(
        season_code=str(season_code),
        player_count=len(players),
        avg_overall=avg_overall,
        median_overall=median_overall,
        best=best,
        worst=worst,
        category_averages=category_averages,
    )


# ---------------------------------------------------------------------------
# Combine Score Leaders (for stats homepage)
# ---------------------------------------------------------------------------

COMBINE_LEADER_SCORE_TYPES = [
    "combine_score_overall",
    "combine_score_anthropometrics",
    "combine_score_athletic",
    "combine_score_shooting",
]


@dataclass
class CombineScoreLeaderEntry:
    """A single player entry in a combine score leaderboard."""

    player_id: int
    display_name: str
    slug: str
    position: Optional[str]
    school: Optional[str]
    draft_year: Optional[int]
    percentile: float
    rank: int
    category_scores: Dict[str, float] = field(default_factory=dict)


async def _find_most_recent_combine_season(db: AsyncSession) -> Optional[int]:
    """Find the season_id of the most recent combine_score snapshot."""
    result = await db.execute(
        select(MetricSnapshot.season_id)  # type: ignore[call-overload]
        .where(
            cast(Any, MetricSnapshot.source) == MetricSource.combine_score,
            cast(Any, MetricSnapshot.cohort) == CohortType.current_draft,
            cast(Any, MetricSnapshot.is_current).is_(True),
            cast(Any, MetricSnapshot.position_scope_parent).is_(None),
            cast(Any, MetricSnapshot.position_scope_fine).is_(None),
        )
        .join(Season, MetricSnapshot.season_id == Season.id)  # type: ignore[arg-type]
        .order_by(desc(Season.end_year))  # type: ignore[arg-type]
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_combine_score_leaders(
    db: AsyncSession,
    limit: int = 5,
    season_id: Optional[int] = None,
) -> Dict[str, List[CombineScoreLeaderEntry]]:
    """Fetch top players by combine score for each score type.

    Args:
        db: Async database session.
        limit: Number of leaders per score type.
        season_id: Specific season, or None to auto-detect most recent.

    Returns:
        Dict keyed by score type (e.g. 'combine_score_overall'), each
        containing a list of CombineScoreLeaderEntry sorted by percentile
        descending. Returns empty dict if no data.
    """
    if season_id is None:
        season_id = await _find_most_recent_combine_season(db)
    if season_id is None:
        return {}

    all_players = await get_year_combine_scores(db, season_id)
    if not all_players:
        return {}

    # Resolve season end_year for draft_year display
    season_result = await db.execute(
        select(Season.end_year).where(  # type: ignore[call-overload]
            Season.id == season_id  # type: ignore[arg-type]
        )
    )
    draft_year = season_result.scalar_one_or_none()

    # Bulk-fetch positions from combine_anthro for these players
    player_ids = [p.player_id for p in all_players]
    pos_result = await db.execute(
        select(
            CombineAnthro.player_id,
            Position.code,  # type: ignore[call-overload]
        )
        .outerjoin(
            Position,
            CombineAnthro.position_id == Position.id,  # type: ignore[arg-type]
        )
        .where(
            CombineAnthro.player_id.in_(player_ids),  # type: ignore[union-attr,attr-defined]
            CombineAnthro.season_id == season_id,  # type: ignore[arg-type]
        )
    )
    position_map: Dict[int, str] = {}
    for row in pos_result.mappings().all():
        pid = row["player_id"]
        code = row["code"]
        if pid is not None and code is not None and pid not in position_map:
            position_map[int(pid)] = str(code)

    # Bulk-fetch school from PlayerMaster
    school_result = await db.execute(
        select(
            PlayerMaster.id,
            PlayerMaster.school,  # type: ignore[call-overload]
        ).where(PlayerMaster.id.in_(player_ids))  # type: ignore[union-attr]
    )
    school_map: Dict[int, Optional[str]] = {
        int(r["id"]): r["school"] for r in school_result.mappings().all() if r["id"]
    }

    leaders: Dict[str, List[CombineScoreLeaderEntry]] = {}

    for score_type in COMBINE_LEADER_SCORE_TYPES:
        scored: list[tuple[float, PlayerCombineScores]] = []
        for p in all_players:
            if score_type == "combine_score_overall":
                entry = p.overall_score
            else:
                entry = p.category_scores.get(score_type)
            if entry and entry.percentile is not None:
                scored.append((entry.percentile, p))

        scored.sort(key=lambda x: x[0], reverse=True)

        entries: list[CombineScoreLeaderEntry] = []
        for rank_idx, (pctl, player) in enumerate(scored[:limit], start=1):
            cat_scores: Dict[str, float] = {}
            if score_type == "combine_score_overall":
                for cat_key in [
                    "combine_score_anthropometrics",
                    "combine_score_athletic",
                    "combine_score_shooting",
                ]:
                    cat_entry = player.category_scores.get(cat_key)
                    if cat_entry and cat_entry.percentile is not None:
                        cat_scores[cat_key] = cat_entry.percentile

            entries.append(
                CombineScoreLeaderEntry(
                    player_id=player.player_id,
                    display_name=player.player_name or "",
                    slug=player.player_slug or "",
                    position=position_map.get(player.player_id),
                    school=school_map.get(player.player_id),
                    draft_year=draft_year,
                    percentile=pctl,
                    rank=rank_idx,
                    category_scores=cat_scores,
                )
            )

        if entries:
            leaders[score_type] = entries

    return leaders
