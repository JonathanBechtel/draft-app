"""Find non-obvious facts to seed an OUTLIER thread.

Three independent query patterns are tried in random order:

1. ``split_profile``  — top-decile in one metric AND bottom-decile in another.
2. ``elite_metric``   — at least one metric at the 99th percentile or higher.
3. ``plus_wingspan``  — wingspan minus height-with-shoes is a top-quintile gap.

Each returns one randomly-chosen candidate; the picker that wraps this call
respects the dedup window so we don't keep mining the same player.
"""

from __future__ import annotations

import random
from typing import Any, Optional, cast

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import CohortType
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.metrics import MetricDefinition, MetricSnapshot, PlayerMetricValue
from app.schemas.players_master import PlayerMaster

from .types import OutlierResult, PlayerCard, StatFact


async def _current_player_pool(db: AsyncSession, excluded: set[int]) -> list[int]:
    """Return current-draft-class player IDs (with anthro data), minus excluded.

    Latest year is computed against players who actually have anthro rows, so
    stub future-draft-year entries in players_master don't blow the pool away.
    """
    stmt = (
        select(func.max(PlayerMaster.draft_year))  # type: ignore[arg-type]
        .select_from(PlayerMaster)
        .join(CombineAnthro, CombineAnthro.player_id == PlayerMaster.id)  # type: ignore[arg-type]
        .where(PlayerMaster.is_stub.is_(False))  # type: ignore[attr-defined]
    )
    res = await db.execute(stmt)
    draft_year: Optional[int] = res.scalar()
    if draft_year is None:
        return []

    stmt2 = (
        select(PlayerMaster.id)  # type: ignore[call-overload]
        .join(CombineAnthro, CombineAnthro.player_id == PlayerMaster.id)  # type: ignore[arg-type]
        .where(PlayerMaster.draft_year == draft_year)  # type: ignore[arg-type]
        .where(PlayerMaster.is_stub.is_(False))  # type: ignore[attr-defined]
        .where(PlayerMaster.slug.is_not(None))  # type: ignore[union-attr]
    )
    if excluded:
        stmt2 = stmt2.where(PlayerMaster.id.notin_(excluded))  # type: ignore[union-attr]
    result = await db.execute(stmt2)
    return [row[0] for row in result.all()]


async def _split_profile(db: AsyncSession, pool: list[int]) -> Optional[OutlierResult]:
    """Player with elite (>=90) AND deficient (<=20) percentiles in different metrics."""
    if not pool:
        return None

    # Pull all metric rows for the current_draft cohort for the pool players.
    high_stmt = (
        select(  # type: ignore[call-overload]
            PlayerMetricValue.player_id,
            MetricDefinition.display_name,
            PlayerMetricValue.percentile,
            PlayerMetricValue.raw_value,
            PlayerMetricValue.rank,
            MetricSnapshot.population_size,
            MetricDefinition.unit,
        )
        .join(
            MetricDefinition,
            MetricDefinition.id == PlayerMetricValue.metric_definition_id,
        )
        .join(MetricSnapshot, MetricSnapshot.id == PlayerMetricValue.snapshot_id)
        .where(MetricSnapshot.cohort == CohortType.current_draft)  # type: ignore[arg-type]
        .where(MetricSnapshot.is_current.is_(True))  # type: ignore[attr-defined]
        .where(PlayerMetricValue.player_id.in_(pool))  # type: ignore[attr-defined]
        .where(PlayerMetricValue.percentile.is_not(None))  # type: ignore[union-attr]
    )
    result = await db.execute(high_stmt)
    rows = cast(list[Any], result.all())

    # Group rows by player and pick those with both extremes.
    by_player: dict[int, list[Any]] = {}
    for row in rows:
        by_player.setdefault(int(row.player_id), []).append(row)

    candidates: list[tuple[int, Any, Any]] = []
    for player_id, prows in by_player.items():
        highs = [r for r in prows if r.percentile is not None and r.percentile >= 90]
        lows = [r for r in prows if r.percentile is not None and r.percentile <= 20]
        if highs and lows:
            candidates.append((player_id, random.choice(highs), random.choice(lows)))

    if not candidates:
        return None

    player_id, high_row, low_row = random.choice(candidates)
    player = await _player_card(db, player_id)
    if player is None:
        return None

    stats = [
        StatFact(
            label=str(high_row.display_name),
            value=_fmt_value(high_row.raw_value, high_row.unit),
            percentile=float(high_row.percentile)
            if high_row.percentile is not None
            else None,
            rank=int(high_row.rank) if high_row.rank is not None else None,
            population_size=(
                int(high_row.population_size)
                if high_row.population_size is not None
                else None
            ),
            context="top of class",
        ),
        StatFact(
            label=str(low_row.display_name),
            value=_fmt_value(low_row.raw_value, low_row.unit),
            percentile=float(low_row.percentile)
            if low_row.percentile is not None
            else None,
            rank=int(low_row.rank) if low_row.rank is not None else None,
            population_size=(
                int(low_row.population_size)
                if low_row.population_size is not None
                else None
            ),
            context="bottom tier",
        ),
    ]
    return OutlierResult(
        player=player,
        subtype="split_profile",
        headline=f"{player.display_name} — high-variance profile",
        stats=stats,
        support_text=(
            f"{player.display_name} pairs an elite mark in {high_row.display_name} "
            f"with a bottom-tier number in {low_row.display_name} — the kind of "
            "split scouts argue over."
        ),
    )


async def _elite_metric(db: AsyncSession, pool: list[int]) -> Optional[OutlierResult]:
    """A player with a single >=99th percentile metric in the current class."""
    if not pool:
        return None
    stmt = (
        select(  # type: ignore[call-overload]
            PlayerMetricValue.player_id,
            MetricDefinition.display_name,
            PlayerMetricValue.percentile,
            PlayerMetricValue.raw_value,
            PlayerMetricValue.rank,
            MetricSnapshot.population_size,
            MetricDefinition.unit,
        )
        .join(
            MetricDefinition,
            MetricDefinition.id == PlayerMetricValue.metric_definition_id,
        )
        .join(MetricSnapshot, MetricSnapshot.id == PlayerMetricValue.snapshot_id)
        .where(MetricSnapshot.cohort == CohortType.current_draft)  # type: ignore[arg-type]
        .where(MetricSnapshot.is_current.is_(True))  # type: ignore[attr-defined]
        .where(PlayerMetricValue.player_id.in_(pool))  # type: ignore[attr-defined]
        .where(PlayerMetricValue.percentile >= 99)  # type: ignore[operator]
        .order_by(func.random())
        .limit(25)
    )
    result = await db.execute(stmt)
    rows = cast(list[Any], result.all())
    if not rows:
        return None

    chosen = random.choice(rows)
    player = await _player_card(db, int(chosen.player_id))
    if player is None:
        return None

    fact = StatFact(
        label=str(chosen.display_name),
        value=_fmt_value(chosen.raw_value, chosen.unit),
        percentile=float(chosen.percentile) if chosen.percentile is not None else None,
        rank=int(chosen.rank) if chosen.rank is not None else None,
        population_size=(
            int(chosen.population_size) if chosen.population_size is not None else None
        ),
        context="99th percentile or better",
    )
    return OutlierResult(
        player=player,
        subtype="elite_metric",
        headline=f"{player.display_name} — 99th percentile {chosen.display_name}",
        stats=[fact],
        support_text=(
            f"{player.display_name}'s {chosen.display_name} sits in the 99th percentile "
            "of the current draft class — a number worth flagging."
        ),
    )


async def _plus_wingspan(db: AsyncSession, pool: list[int]) -> Optional[OutlierResult]:
    """A player whose wingspan minus shoe-height differential is in the top 20%."""
    if not pool:
        return None

    stmt = (
        select(  # type: ignore[call-overload]
            CombineAnthro.player_id,
            CombineAnthro.wingspan_in,
            CombineAnthro.height_w_shoes_in,
        )
        .where(CombineAnthro.player_id.in_(pool))  # type: ignore[attr-defined]
        .where(CombineAnthro.wingspan_in.is_not(None))  # type: ignore[union-attr]
        .where(CombineAnthro.height_w_shoes_in.is_not(None))  # type: ignore[union-attr]
    )
    result = await db.execute(stmt)
    rows = [
        (int(r.player_id), float(r.wingspan_in), float(r.height_w_shoes_in))
        for r in result.all()
    ]
    if len(rows) < 5:
        return None

    diffs = [(pid, ws, ht, ws - ht) for pid, ws, ht in rows]
    diffs.sort(key=lambda x: x[3], reverse=True)
    top_count = max(1, len(diffs) // 5)
    top = diffs[:top_count]

    pid, ws, ht, diff = random.choice(top)
    player = await _player_card(db, pid)
    if player is None:
        return None

    diff_inches = round(diff * 2) / 2  # half-inch precision
    rank_in_class = next((i + 1 for i, row in enumerate(diffs) if row[0] == pid), None)

    facts = [
        StatFact(
            label="Wingspan",
            value=_inches_to_feet(ws),
            context="combine measurement",
        ),
        StatFact(
            label="Height (shoes)",
            value=_inches_to_feet(ht),
        ),
        StatFact(
            label="Plus-wingspan",
            value=f'+{diff_inches:g}"',
            rank=rank_in_class,
            population_size=len(diffs),
            context="top 20% of class",
        ),
    ]
    return OutlierResult(
        player=player,
        subtype="plus_wingspan",
        headline=f"{player.display_name} — plus-wingspan frame",
        stats=facts,
        support_text=(
            f'{player.display_name} carries a +{diff_inches:g}" wingspan over standing '
            f"height — ranked {rank_in_class} of {len(diffs)} prospects with anthro data."
        ),
    )


_FINDERS = (_split_profile, _elite_metric, _plus_wingspan)


async def find_outlier_candidate(
    db: AsyncSession,
    *,
    excluded_player_ids: Optional[set[int]] = None,
) -> Optional[OutlierResult]:
    """Run the finders in random order and return the first non-empty result."""
    excluded = excluded_player_ids or set()
    pool = await _current_player_pool(db, excluded)
    if not pool:
        return None

    finders = list(_FINDERS)
    random.shuffle(finders)
    for finder in finders:
        result = await finder(db, pool)
        if result is not None:
            return result
    return None


async def _player_card(db: AsyncSession, player_id: int) -> Optional[PlayerCard]:
    stmt = select(  # type: ignore[call-overload]
        PlayerMaster.id,
        PlayerMaster.slug,
        PlayerMaster.display_name,
        PlayerMaster.school,
        PlayerMaster.draft_year,
    ).where(and_(PlayerMaster.id == player_id))  # type: ignore[arg-type,misc]
    result = await db.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None
    return PlayerCard(
        id=row.id,
        slug=row.slug or "",
        display_name=row.display_name or "",
        school=row.school,
        draft_year=row.draft_year,
    )


def _fmt_value(raw: Optional[float], unit: Optional[str]) -> str:
    if raw is None:
        return "—"
    if unit == "inches":
        return _inches_to_feet(raw)
    if unit == "percent":
        return f"{raw:.1f}%"
    if unit == "pounds":
        return f"{raw:.0f} lbs"
    if unit == "seconds":
        return f"{raw:.2f} sec"
    return f"{raw:g}"


def _inches_to_feet(raw_inches: float) -> str:
    rounded = round(raw_inches * 2) / 2
    feet = int(rounded) // 12
    inches = rounded % 12
    if inches == int(inches):
        return f"{feet}'{int(inches)}\""
    return f"{feet}'{inches}\""
