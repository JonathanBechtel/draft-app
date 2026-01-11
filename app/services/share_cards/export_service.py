"""Image export service orchestrating the full share card generation pipeline."""

import logging
import time
from dataclasses import asdict
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.share_cards.cache_keys import (
    generate_cache_key,
    generate_filename,
    generate_title,
)
from app.services.share_cards.model_builders import (
    build_comps_model,
    build_h2h_model,
    build_performance_model,
    build_vs_arena_model,
)
from app.services.share_cards.rasterizer import get_rasterizer
from app.services.share_cards.render_models import RenderModel
from app.services.share_cards.storage import get_export_storage
from app.services.share_cards.svg_renderer import get_svg_renderer

logger = logging.getLogger(__name__)

ComponentType = Literal["vs_arena", "performance", "h2h", "comps"]

# Map component types to template files
COMPONENT_TEMPLATES = {
    "vs_arena": "vs_arena.svg",
    "performance": "performance.svg",
    "h2h": "h2h.svg",
    "comps": "comps.svg",
}


class ImageExportService:
    """Orchestrates share card generation from data to PNG."""

    def __init__(self, db: AsyncSession) -> None:
        """Initialize the export service.

        Args:
            db: Async database session
        """
        self.db = db
        self.storage = get_export_storage()
        self.renderer = get_svg_renderer()
        self.rasterizer = get_rasterizer()

    async def export(
        self,
        component: ComponentType,
        player_ids: list[int],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate a shareable PNG image for the specified component.

        Args:
            component: Component type (vs_arena, performance, h2h, comps)
            player_ids: List of player IDs involved
            context: Export context (comparison_group, same_position, metric_group)

        Returns:
            Dict with url, title, filename, cached fields

        Raises:
            ValueError: If player not found or invalid component
        """
        start_time = time.perf_counter()

        # Generate cache key
        cache_key = generate_cache_key(component, player_ids, context)

        # Check cache first
        cached_url = self.storage.check_cache(cache_key)
        if cached_url:
            # Still need player names for response
            model = await self._build_model(component, player_ids, context)
            player_names = self._extract_player_names(model)

            logger.info(
                f"Export cache hit: component={component}, key={cache_key}, "
                f"duration={time.perf_counter() - start_time:.3f}s"
            )

            return {
                "url": cached_url,
                "title": generate_title(component, player_names),
                "filename": generate_filename(component, player_names),
                "cached": True,
            }

        # Cache miss - generate image
        model_start = time.perf_counter()
        model = await self._build_model(component, player_ids, context)
        model_duration = time.perf_counter() - model_start

        render_start = time.perf_counter()
        svg_content = self._render_svg(component, model)
        render_duration = time.perf_counter() - render_start

        raster_start = time.perf_counter()
        png_bytes = self.rasterizer.rasterize(svg_content)
        raster_duration = time.perf_counter() - raster_start

        upload_start = time.perf_counter()
        url = self.storage.upload(cache_key, png_bytes)
        upload_duration = time.perf_counter() - upload_start

        total_duration = time.perf_counter() - start_time

        player_names = self._extract_player_names(model)

        logger.info(
            f"Export generated: component={component}, key={cache_key}, "
            f"size={len(png_bytes)} bytes, "
            f"model={model_duration:.3f}s, render={render_duration:.3f}s, "
            f"raster={raster_duration:.3f}s, upload={upload_duration:.3f}s, "
            f"total={total_duration:.3f}s"
        )

        return {
            "url": url,
            "title": generate_title(component, player_names),
            "filename": generate_filename(component, player_names),
            "cached": False,
        }

    async def _build_model(
        self,
        component: ComponentType,
        player_ids: list[int],
        context: dict[str, Any],
    ) -> RenderModel:
        """Build the appropriate render model for a component.

        Args:
            component: Component type
            player_ids: Player IDs
            context: Export context

        Returns:
            Render model dataclass

        Raises:
            ValueError: If unknown component or invalid player_ids
        """
        if component == "vs_arena":
            return await build_vs_arena_model(self.db, player_ids, context)
        elif component == "performance":
            return await build_performance_model(self.db, player_ids, context)
        elif component == "h2h":
            return await build_h2h_model(self.db, player_ids, context)
        elif component == "comps":
            return await build_comps_model(self.db, player_ids, context)
        else:
            raise ValueError(f"Unknown component: {component}")

    def _render_svg(self, component: ComponentType, model: RenderModel) -> str:
        """Render SVG from model.

        Args:
            component: Component type
            model: Render model dataclass

        Returns:
            SVG markup string
        """
        template_name = COMPONENT_TEMPLATES.get(component)
        if not template_name:
            raise ValueError(f"No template for component: {component}")

        # Convert dataclass to dict for template context
        context = asdict(model)

        # asdict() doesn't include @property methods, so add rendered context line
        if "context_line" in context:
            context["context_line"]["rendered"] = model.context_line.rendered

        return self.renderer.render(template_name, context)

    def _extract_player_names(self, model: RenderModel) -> list[str]:
        """Extract player names from render model for filename/title generation."""
        from app.services.share_cards.render_models import (
            CompsRenderModel,
            H2HRenderModel,
            PerformanceRenderModel,
            VSArenaRenderModel,
        )

        if isinstance(model, (VSArenaRenderModel, H2HRenderModel)):
            return [model.player_a.name, model.player_b.name]
        elif isinstance(model, (PerformanceRenderModel, CompsRenderModel)):
            return [model.player.name]
        else:
            return ["Player"]
