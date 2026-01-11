"""SVG template rendering with Jinja2."""

import logging
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.services.share_cards.constants import COLORS, FONTS, LAYOUT
from app.services.share_cards.image_embedder import render_name_placeholder_svg

logger = logging.getLogger(__name__)

# Template directory path
SVG_TEMPLATE_DIR = Path(__file__).parent.parent.parent / "templates" / "export_svg"


class SVGRenderer:
    """Renders SVG templates with Jinja2."""

    def __init__(self) -> None:
        """Initialize the Jinja2 environment for SVG rendering."""
        self.env = Environment(
            loader=FileSystemLoader(str(SVG_TEMPLATE_DIR)),
            autoescape=select_autoescape(["svg", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

        # Add custom filters
        self.env.filters["escape_xml"] = _escape_xml

        # Add global template variables
        self.env.globals["colors"] = COLORS
        self.env.globals["fonts"] = FONTS
        self.env.globals["layout"] = LAYOUT
        self.env.globals["render_name_placeholder"] = render_name_placeholder_svg

    def render(self, template_name: str, context: dict[str, Any]) -> str:
        """Render an SVG template with the given context.

        Args:
            template_name: Template filename (e.g., "performance.svg")
            context: Template context variables

        Returns:
            Rendered SVG string
        """
        template = self.env.get_template(template_name)
        return template.render(**context)


def _escape_xml(text: str) -> str:
    """Escape special XML characters for safe SVG rendering."""
    if not isinstance(text, str):
        text = str(text)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# Module-level singleton for convenience
_renderer: SVGRenderer | None = None


def get_svg_renderer() -> SVGRenderer:
    """Get or create the SVG renderer singleton."""
    global _renderer
    if _renderer is None:
        _renderer = SVGRenderer()
    return _renderer
