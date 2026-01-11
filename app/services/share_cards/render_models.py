"""Render model dataclasses for share card SVG templates."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from app.services.share_cards.constants import TEMPLATE_VERSION


class PercentileTier(str, Enum):
    """Tier classification based on percentile value."""

    elite = "elite"  # 90+
    good = "good"  # 70-89
    average = "average"  # 40-69
    below = "below"  # 0-39
    unknown = "unknown"


class WinnerSide(str, Enum):
    """Winner designation for comparison rows."""

    a = "a"
    b = "b"
    tie = "tie"
    none = "none"


@dataclass
class PlayerBadge:
    """Player identity for share card headers."""

    name: str  # Display name, may be ellipsized
    subtitle: str  # e.g., "F | Duke (2025)"
    photo_data_uri: Optional[str] = None  # base64 embedded image
    has_photo: bool = True  # False = show name placeholder


@dataclass
class ContextLine:
    """Filter context shown below title."""

    comparison_group_label: str  # e.g., "Current Draft Class"
    position_filter_label: str  # e.g., "Same Position" or "All Positions"
    metric_group_label: str  # e.g., "Anthropometrics"

    @property
    def rendered(self) -> str:
        """Pre-formatted string with middle dots."""
        parts = [
            self.comparison_group_label,
            self.position_filter_label,
            self.metric_group_label,
        ]
        return " · ".join(p for p in parts if p)


@dataclass
class VSRow:
    """Single comparison row for VS Arena / H2H."""

    label: str
    a_value: str
    b_value: str
    winner: WinnerSide = WinnerSide.none
    lower_is_better: bool = False


@dataclass
class PerformanceRow:
    """Single percentile bar row."""

    label: str
    value: str
    percentile: int  # 0-100
    percentile_label: str  # e.g., "92nd"
    tier: PercentileTier = PercentileTier.unknown


@dataclass
class CompTile:
    """Single comparison player tile."""

    name: str
    subtitle: str  # pos/school/year
    similarity: int  # 0-100
    similarity_label: str  # e.g., "87%"
    photo_data_uri: Optional[str] = None
    has_photo: bool = True
    tier: PercentileTier = PercentileTier.good


@dataclass
class VSArenaRenderModel:
    """VS Arena share card (6 rows max)."""

    title: str
    context_line: ContextLine
    player_a: PlayerBadge
    player_b: PlayerBadge
    rows: list[VSRow] = field(default_factory=list)
    accent_color: str = "#d946ef"  # fuchsia
    template_version: str = TEMPLATE_VERSION


@dataclass
class PerformanceRenderModel:
    """Performance metrics share card (8 rows max)."""

    title: str
    context_line: ContextLine
    player: PlayerBadge
    rows: list[PerformanceRow] = field(default_factory=list)
    accent_color: str = "#10b981"  # emerald
    template_version: str = TEMPLATE_VERSION


@dataclass
class H2HRenderModel:
    """Head-to-head comparison (8 rows max)."""

    title: str
    context_line: ContextLine
    player_a: PlayerBadge
    player_b: PlayerBadge
    similarity_badge: Optional[str] = None  # e.g., "92% Match"
    rows: list[VSRow] = field(default_factory=list)
    accent_color: str = "#d946ef"  # fuchsia
    template_version: str = TEMPLATE_VERSION


@dataclass
class CompsRenderModel:
    """Player comparisons share card (6 tiles, 2x3 grid)."""

    title: str
    context_line: ContextLine
    player: PlayerBadge
    tiles: list[CompTile] = field(default_factory=list)
    accent_color: str = "#06b6d4"  # cyan
    template_version: str = TEMPLATE_VERSION


# Type alias for all render model types
RenderModel = (
    VSArenaRenderModel | PerformanceRenderModel | H2HRenderModel | CompsRenderModel
)
