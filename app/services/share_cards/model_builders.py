"""Build render models from database data for share card generation."""

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fields import CohortType, MetricCategory, SimilarityDimension
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.positions import Position
from app.services.head_to_head_service import get_head_to_head_comparison
from app.services.image_assets_service import get_current_image_url_for_player
from app.services.metrics_service import get_player_metrics
from app.services.share_cards.constants import (
    COMPARISON_GROUP_LABELS,
    COMPONENT_ACCENTS,
    LIST_LENGTHS,
    METRIC_GROUP_LABELS,
    get_percentile_tier,
)
from app.services.share_cards.image_embedder import fetch_and_embed_image
from app.services.share_cards.render_models import (
    CompTile,
    CompsRenderModel,
    ContextLine,
    H2HRenderModel,
    PercentileTier,
    PerformanceRenderModel,
    PerformanceRow,
    PlayerBadge,
    VSArenaRenderModel,
    VSRow,
    WinnerSide,
)
from app.services.similarity_service import get_similar_players

# Label mappings for export cards (shorten long labels to fit)
EXPORT_LABEL_MAP = {
    # Anthropometrics
    "Height (Without Shoes)": "Height (Barefoot)",
    "Height (With Shoes)": "Height (Shoes)",
    # Combine
    "Three-Quarter Sprint": "3/4 Sprint",
    "Lane Agility Time": "Lane Agility",
    # Shooting
    "Three-Point Star FG%": "3PT Star FG%",
    "Corner Three FG%": "Corner 3 FG%",
}


def _shorten_label(label: str) -> str:
    """Shorten metric label for export cards if needed."""
    return EXPORT_LABEL_MAP.get(label, label)


async def _resolve_player_info(
    db: AsyncSession, player_id: int
) -> tuple[str, str, str, Optional[str], Optional[list[str]], Optional[int]]:
    """Resolve player name, slug, subtitle, image URL, position parents, and draft year.

    Returns:
        Tuple of (display_name, slug, subtitle, image_url, position_parents, draft_year)
    """
    stmt = (
        select(  # type: ignore[call-overload, misc]
            PlayerMaster.display_name,
            PlayerMaster.slug,
            PlayerMaster.school,
            PlayerMaster.draft_year,
            Position.code,
            Position.parents,
        )
        .select_from(PlayerMaster)
        .outerjoin(PlayerStatus, PlayerStatus.player_id == PlayerMaster.id)
        .outerjoin(Position, Position.id == PlayerStatus.position_id)
        .where(PlayerMaster.id == player_id)
    )
    result = await db.execute(stmt)
    row = result.one_or_none()

    if not row:
        raise ValueError("player_not_found")

    display_name = row.display_name
    slug = row.slug
    position = row.code or ""
    school = row.school or ""
    draft_year = row.draft_year
    position_parents = row.parents

    # Build subtitle like "F | Duke (2025)"
    parts = []
    if position:
        parts.append(position.upper())
    if school:
        parts.append(school)
    if draft_year:
        parts.append(f"({draft_year})")

    subtitle = " | ".join(parts[:2])
    if draft_year and len(parts) > 2:
        subtitle = f"{subtitle} {parts[-1]}"

    # Get image URL
    image_url = await get_current_image_url_for_player(
        db, player_id=player_id, style="default"
    )

    return display_name, slug, subtitle, image_url, position_parents, draft_year


async def _build_player_badge(db: AsyncSession, player_id: int) -> PlayerBadge:
    """Build a PlayerBadge with embedded image."""
    display_name, _, subtitle, image_url, _, _ = await _resolve_player_info(
        db, player_id
    )

    photo_uri, has_photo = await fetch_and_embed_image(image_url, display_name)

    return PlayerBadge(
        name=display_name,
        subtitle=subtitle,
        photo_data_uri=photo_uri,
        has_photo=has_photo,
    )


def _build_context_line(
    context: dict[str, Any],
    position_parents: Optional[list[str]] = None,
    draft_year: Optional[int] = None,
) -> ContextLine:
    """Build context line from export context.

    Args:
        context: Export context with comparison_group, same_position, metric_group
        position_parents: List of parent position groups (e.g., ["guard", "wing"])
        draft_year: Player's draft year for dynamic labeling
    """
    comparison_group = context.get("comparison_group", "current_draft")
    same_position = context.get("same_position", False)
    metric_group = context.get("metric_group", "anthropometrics")

    # Build comparison group label (with dynamic year for current_draft)
    comparison_label = COMPARISON_GROUP_LABELS.get(comparison_group, "")
    if comparison_group == "current_draft" and draft_year:
        comparison_label = f"Compared to {draft_year} Draft Class"

    # Use position parent group when same_position is True
    if same_position and position_parents:
        # Use first parent group, capitalize and pluralize (e.g., "guard" → "Guards Only")
        parent = position_parents[0].capitalize()
        position_label = f"{parent}s Only"
    else:
        position_label = "All Positions"

    return ContextLine(
        comparison_group_label=comparison_label,
        position_filter_label=position_label,
        metric_group_label=METRIC_GROUP_LABELS.get(metric_group, ""),
    )


def _determine_winner(
    val_a: Optional[float],
    val_b: Optional[float],
    lower_is_better: bool,
) -> WinnerSide:
    """Determine winner between two values."""
    if val_a is None or val_b is None:
        return WinnerSide.none

    if val_a == val_b:
        return WinnerSide.tie

    if lower_is_better:
        return WinnerSide.a if val_a < val_b else WinnerSide.b
    else:
        return WinnerSide.a if val_a > val_b else WinnerSide.b


def _format_percentile_label(percentile: int) -> str:
    """Format percentile as percentage (e.g., '92%')."""
    return f"{percentile}%"


async def build_vs_arena_model(
    db: AsyncSession,
    player_ids: list[int],
    context: dict[str, Any],
) -> VSArenaRenderModel:
    """Build VS Arena render model from player IDs and context.

    Args:
        db: Database session
        player_ids: List of two player IDs
        context: Export context with metric_group, etc.

    Returns:
        VSArenaRenderModel ready for template rendering
    """
    if len(player_ids) != 2:
        raise ValueError("VS Arena requires exactly 2 player IDs")

    # Resolve player info (use player A's position/year for context)
    name_a, slug_a, _, _, position_parents, draft_year = await _resolve_player_info(
        db, player_ids[0]
    )
    name_b, slug_b, _, _, _, _ = await _resolve_player_info(db, player_ids[1])

    # Build player badges
    player_a = await _build_player_badge(db, player_ids[0])
    player_b = await _build_player_badge(db, player_ids[1])

    # Map metric_group to category
    metric_group = context.get("metric_group", "anthropometrics")
    category_map = {
        "anthropometrics": MetricCategory.anthropometrics,
        "combine": MetricCategory.combine_performance,
        "shooting": MetricCategory.shooting,
    }
    category = category_map.get(metric_group, MetricCategory.anthropometrics)

    # Fetch H2H data
    h2h_data = await get_head_to_head_comparison(db, slug_a, slug_b, category)

    # Build rows (limited to LIST_LENGTHS["vs_arena"])
    max_rows = LIST_LENGTHS["vs_arena"]
    rows: list[VSRow] = []

    for metric in h2h_data.get("metrics", [])[:max_rows]:
        raw_a = metric.get("raw_value_a")
        raw_b = metric.get("raw_value_b")
        lower_is_better = metric.get("lower_is_better", False)
        unit = metric.get("unit", "")

        winner = _determine_winner(raw_a, raw_b, lower_is_better)

        # Append unit to display values if present
        display_a = metric.get("display_value_a") or "—"
        display_b = metric.get("display_value_b") or "—"
        if unit and display_a != "—":
            display_a = f"{display_a}{unit}"
        if unit and display_b != "—":
            display_b = f"{display_b}{unit}"

        rows.append(
            VSRow(
                label=metric.get("metric", ""),
                a_value=display_a,
                b_value=display_b,
                winner=winner,
                lower_is_better=lower_is_better,
            )
        )

    # Pad with empty rows if needed
    while len(rows) < max_rows:
        rows.append(VSRow(label="", a_value="—", b_value="—"))

    return VSArenaRenderModel(
        title=f"{name_a} vs {name_b}",
        context_line=_build_context_line(context, position_parents, draft_year),
        player_a=player_a,
        player_b=player_b,
        rows=rows,
        accent_color=COMPONENT_ACCENTS["vs_arena"],
    )


async def build_performance_model(
    db: AsyncSession,
    player_ids: list[int],
    context: dict[str, Any],
) -> PerformanceRenderModel:
    """Build Performance render model from player ID and context.

    Args:
        db: Database session
        player_ids: List with single player ID
        context: Export context with comparison_group, metric_group, etc.

    Returns:
        PerformanceRenderModel ready for template rendering
    """
    if len(player_ids) != 1:
        raise ValueError("Performance requires exactly 1 player ID")

    player_id = player_ids[0]

    # Resolve player info
    name, slug, _, _, position_parents, draft_year = await _resolve_player_info(
        db, player_id
    )
    player = await _build_player_badge(db, player_id)

    # Map context to service parameters
    comparison_group = context.get("comparison_group", "current_draft")
    metric_group = context.get("metric_group", "anthropometrics")

    cohort_map = {
        "current_draft": CohortType.current_draft,
        "current_nba": CohortType.current_nba,
        "all_time_draft": CohortType.all_time_draft,
        "all_time_nba": CohortType.all_time_nba,
    }
    cohort = cohort_map.get(comparison_group, CohortType.current_draft)

    category_map = {
        "anthropometrics": MetricCategory.anthropometrics,
        "combine": MetricCategory.combine_performance,
        "shooting": MetricCategory.shooting,
    }
    category = category_map.get(metric_group, MetricCategory.anthropometrics)

    position_adjusted = context.get("same_position", False)

    # Fetch metrics
    metrics_result = await get_player_metrics(
        db, slug, cohort, category, position_adjusted=position_adjusted
    )

    # Build rows
    max_rows = LIST_LENGTHS["performance"]
    rows: list[PerformanceRow] = []

    for metric in metrics_result.get("metrics", [])[:max_rows]:
        percentile = metric.get("percentile")
        if percentile is None:
            percentile = 0
            tier = PercentileTier.unknown
        else:
            tier = PercentileTier(get_percentile_tier(percentile))

        value = metric.get("value") or "—"
        unit = metric.get("unit", "")
        if unit and value != "—":
            value = f"{value}{unit}"

        rows.append(
            PerformanceRow(
                label=_shorten_label(metric.get("metric", "")),
                value=value,
                percentile=percentile,
                percentile_label=_format_percentile_label(percentile),
                tier=tier,
            )
        )

    # Pad with empty rows if needed
    while len(rows) < max_rows:
        rows.append(
            PerformanceRow(
                label="",
                value="—",
                percentile=0,
                percentile_label="—",
                tier=PercentileTier.unknown,
            )
        )

    return PerformanceRenderModel(
        title=f"{name} — Draft Combine Results",
        context_line=_build_context_line(context, position_parents, draft_year),
        player=player,
        rows=rows,
        accent_color=COMPONENT_ACCENTS["performance"],
    )


async def build_h2h_model(
    db: AsyncSession,
    player_ids: list[int],
    context: dict[str, Any],
) -> H2HRenderModel:
    """Build H2H render model from player IDs and context.

    Similar to VS Arena but with similarity badge and more rows.

    Args:
        db: Database session
        player_ids: List of two player IDs
        context: Export context with metric_group, etc.

    Returns:
        H2HRenderModel ready for template rendering
    """
    if len(player_ids) != 2:
        raise ValueError("H2H requires exactly 2 player IDs")

    # Resolve player info (use player A's position/year for context)
    name_a, slug_a, _, _, position_parents, draft_year = await _resolve_player_info(
        db, player_ids[0]
    )
    name_b, slug_b, _, _, _, _ = await _resolve_player_info(db, player_ids[1])

    # Build player badges
    player_a = await _build_player_badge(db, player_ids[0])
    player_b = await _build_player_badge(db, player_ids[1])

    # Map metric_group to category
    metric_group = context.get("metric_group", "anthropometrics")
    category_map = {
        "anthropometrics": MetricCategory.anthropometrics,
        "combine": MetricCategory.combine_performance,
        "shooting": MetricCategory.shooting,
    }
    category = category_map.get(metric_group, MetricCategory.anthropometrics)

    # Fetch H2H data
    h2h_data = await get_head_to_head_comparison(db, slug_a, slug_b, category)

    # Format similarity badge
    # Note: similarity score is already scaled 0-100 in the database
    similarity_badge: Optional[str] = None
    similarity = h2h_data.get("similarity")
    if similarity and similarity.get("score") is not None:
        score = int(similarity["score"])
        similarity_badge = f"{score}% Match"

    # Build rows (limited to LIST_LENGTHS["h2h"])
    max_rows = LIST_LENGTHS["h2h"]
    rows: list[VSRow] = []

    for metric in h2h_data.get("metrics", [])[:max_rows]:
        raw_a = metric.get("raw_value_a")
        raw_b = metric.get("raw_value_b")
        lower_is_better = metric.get("lower_is_better", False)
        unit = metric.get("unit", "")

        winner = _determine_winner(raw_a, raw_b, lower_is_better)

        # Append unit to display values if present
        display_a = metric.get("display_value_a") or "—"
        display_b = metric.get("display_value_b") or "—"
        if unit and display_a != "—":
            display_a = f"{display_a}{unit}"
        if unit and display_b != "—":
            display_b = f"{display_b}{unit}"

        rows.append(
            VSRow(
                label=metric.get("metric", ""),
                a_value=display_a,
                b_value=display_b,
                winner=winner,
                lower_is_better=lower_is_better,
            )
        )

    # Pad with empty rows if needed
    while len(rows) < max_rows:
        rows.append(VSRow(label="", a_value="—", b_value="—"))

    return H2HRenderModel(
        title=f"{name_a} vs {name_b}",
        context_line=_build_context_line(context, position_parents, draft_year),
        player_a=player_a,
        player_b=player_b,
        similarity_badge=similarity_badge,
        rows=rows,
        accent_color=COMPONENT_ACCENTS["h2h"],
    )


async def build_comps_model(
    db: AsyncSession,
    player_ids: list[int],
    context: dict[str, Any],
) -> CompsRenderModel:
    """Build Comps render model from player ID and context.

    Args:
        db: Database session
        player_ids: List with single player ID
        context: Export context with metric_group, same_position, etc.

    Returns:
        CompsRenderModel ready for template rendering
    """
    if len(player_ids) != 1:
        raise ValueError("Comps requires exactly 1 player ID")

    player_id = player_ids[0]

    # Resolve player info
    name, slug, _, _, position_parents, draft_year = await _resolve_player_info(
        db, player_id
    )
    player = await _build_player_badge(db, player_id)

    # Map metric_group to dimension
    metric_group = context.get("metric_group", "anthropometrics")
    dimension_map = {
        "anthropometrics": SimilarityDimension.anthro,
        "combine": SimilarityDimension.combine,
        "shooting": SimilarityDimension.shooting,
    }
    dimension = dimension_map.get(metric_group, SimilarityDimension.anthro)

    same_position = context.get("same_position", False)

    # Fetch similar players
    similar_result = await get_similar_players(
        db,
        slug,
        dimension,
        same_position=same_position,
        limit=LIST_LENGTHS["comps"],
    )

    # Build tiles
    max_tiles = LIST_LENGTHS["comps"]
    tiles: list[CompTile] = []

    for comp in similar_result.get("players", [])[:max_tiles]:
        # Get image for comparison player
        comp_id = comp.get("id")
        comp_name = comp.get("display_name", "")

        image_url = (
            await get_current_image_url_for_player(
                db, player_id=comp_id, style="default"
            )
            if comp_id
            else None
        )

        photo_uri, has_photo = await fetch_and_embed_image(image_url, comp_name)

        # Build subtitle
        position = comp.get("position", "")
        school = comp.get("school", "")
        draft_year = comp.get("draft_year")

        subtitle_parts = []
        if position:
            subtitle_parts.append(position.upper())
        if school:
            subtitle_parts.append(school)
        if draft_year:
            subtitle_parts.append(str(draft_year))

        subtitle = " | ".join(subtitle_parts[:2])

        # Similarity score and tier
        # Note: similarity_score is already scaled 0-100 in the database
        similarity_score = comp.get("similarity_score", 0)
        similarity_pct = int(similarity_score) if similarity_score else 0

        if similarity_pct >= 90:
            tier = PercentileTier.elite
        elif similarity_pct >= 70:
            tier = PercentileTier.good
        elif similarity_pct >= 50:
            tier = PercentileTier.average
        else:
            tier = PercentileTier.below

        tiles.append(
            CompTile(
                name=comp_name,
                subtitle=subtitle,
                similarity=similarity_pct,
                similarity_label=f"{similarity_pct}%",
                photo_data_uri=photo_uri,
                has_photo=has_photo,
                tier=tier,
            )
        )

    # Pad with empty tiles if needed
    while len(tiles) < max_tiles:
        tiles.append(
            CompTile(
                name="—",
                subtitle="",
                similarity=0,
                similarity_label="—",
                has_photo=False,
                tier=PercentileTier.unknown,
            )
        )

    return CompsRenderModel(
        title=f"{name} — Comparisons",
        context_line=_build_context_line(context, position_parents, draft_year),
        player=player,
        tiles=tiles,
        accent_color=COMPONENT_ACCENTS["comps"],
    )
