"""Render images for X threads directly to disk.

Existing share-card primitives (model builders, SVG renderer, rasterizer) are
reused. The S3 upload step in :mod:`app.services.share_cards.export_service`
is intentionally skipped — these PNGs live in the local draft folder.

Custom thread-specific templates live in ``app/templates/x_threads/`` and are
rendered through a separate Jinja environment so they can share the global
filters/globals without polluting the production share-card renderer.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.share_cards.constants import COLORS, FONTS, LAYOUT
from app.services.share_cards.model_builders import (
    build_h2h_model,
    build_performance_model,
)
from app.services.share_cards.rasterizer import get_rasterizer
from app.services.share_cards.svg_renderer import get_svg_renderer

from .types import OutlierResult

logger = logging.getLogger(__name__)


_X_TEMPLATE_DIR = Path(__file__).parent.parent.parent / "templates" / "x_threads"


def _x_threads_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_X_TEMPLATE_DIR)),
        autoescape=select_autoescape(["svg", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["escape_xml"] = _escape_xml
    env.globals["colors"] = COLORS
    env.globals["fonts"] = FONTS
    env.globals["layout"] = LAYOUT
    return env


def _escape_xml(text: Any) -> str:
    if not isinstance(text, str):
        text = str(text)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


async def render_performance_share_card(
    db: AsyncSession,
    *,
    player_id: int,
    output_dir: Path,
) -> Optional[Path]:
    """Render the existing performance share card to a local PNG."""
    try:
        model = await build_performance_model(
            db,
            [player_id],
            {
                "comparison_group": "current_draft",
                "same_position": False,
                "metric_group": "anthropometrics",
            },
        )
    except ValueError as exc:
        logger.warning("performance model build failed: %s", exc)
        return None

    return _render_share_card_to_disk(
        template_name="performance.svg",
        model=model,
        output_dir=output_dir,
        filename=f"performance_{player_id}.png",
    )


async def render_h2h_share_card(
    db: AsyncSession,
    *,
    player_ids: list[int],
    output_dir: Path,
) -> Optional[Path]:
    """Render the existing h2h share card to a local PNG."""
    try:
        model = await build_h2h_model(
            db,
            player_ids,
            {
                "comparison_group": "current_draft",
                "same_position": False,
                "metric_group": "anthropometrics",
            },
        )
    except ValueError as exc:
        logger.warning("h2h model build failed: %s", exc)
        return None

    return _render_share_card_to_disk(
        template_name="h2h.svg",
        model=model,
        output_dir=output_dir,
        filename=f"h2h_{'_'.join(str(p) for p in player_ids)}.png",
    )


def _render_share_card_to_disk(
    *,
    template_name: str,
    model: Any,
    output_dir: Path,
    filename: str,
) -> Optional[Path]:
    renderer = get_svg_renderer()
    rasterizer = get_rasterizer()

    context = asdict(model)
    if "context_line" in context and hasattr(model, "context_line"):
        context["context_line"]["rendered"] = model.context_line.rendered

    svg = renderer.render(template_name, context)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename
    try:
        rasterizer.rasterize_to_file(svg, output_path)
    except Exception as exc:  # noqa: BLE001 — rasterizer can raise many things
        logger.warning("rasterization failed for %s: %s", filename, exc)
        return None
    return output_path


async def render_outlier_card(
    *,
    outlier: OutlierResult,
    output_dir: Path,
) -> Optional[Path]:
    """Render the custom outlier_stat_card.svg with the result data."""
    env = _x_threads_env()
    try:
        template = env.get_template("outlier_stat_card.svg")
    except Exception as exc:  # noqa: BLE001
        logger.warning("outlier template not found: %s", exc)
        return None

    svg = template.render(
        player_name=outlier.player.display_name,
        player_school=outlier.player.school or "",
        headline=outlier.headline,
        subtype=outlier.subtype,
        stats=[
            {
                "label": stat.label,
                "value": stat.value,
                "percentile": stat.percentile,
                "rank": stat.rank,
                "population_size": stat.population_size,
                "context": stat.context,
            }
            for stat in outlier.stats
        ],
        support_text=outlier.support_text,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"outlier_{outlier.player.id}_{outlier.subtype}.png"
    rasterizer = get_rasterizer()
    try:
        rasterizer.rasterize_to_file(svg, output_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("outlier rasterization failed: %s", exc)
        return None
    return output_path
