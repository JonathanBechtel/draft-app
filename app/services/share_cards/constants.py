"""Design tokens, layout constants, and metric definitions for share cards."""

from typing import Tuple

# Template version - bump when export output changes to invalidate cache
TEMPLATE_VERSION = "3"

# Output dimensions
RENDER_WIDTH = 2400  # 2x for crisp text
RENDER_HEIGHT = 1260
OUTPUT_WIDTH = 1200
OUTPUT_HEIGHT = 630

# Design tokens (matching main.css)
COLORS = {
    "slate_900": "#0f172a",
    "slate_800": "#1e293b",
    "slate_700": "#334155",
    "slate_600": "#475569",
    "slate_500": "#64748b",
    "slate_400": "#94a3b8",
    "slate_300": "#cbd5e1",
    "slate_200": "#e2e8f0",
    "slate_100": "#f1f5f9",
    "slate_50": "#f8fafc",
    "white": "#ffffff",
    # Accent colors
    "emerald": "#10b981",
    "emerald_light": "#d1fae5",
    "cyan": "#06b6d4",
    "cyan_light": "#cffafe",
    "fuchsia": "#d946ef",
    "fuchsia_light": "#fae8ff",
    "indigo": "#6366f1",
    "amber": "#f59e0b",
    "amber_light": "#fef3c7",
    "rose": "#f43f5e",
}

# Layout constants (for 2400x1260 canvas at 2x)
LAYOUT = {
    "outer_padding": 96,  # 48px * 2
    "header_height": 240,  # 120px * 2
    "footer_height": 112,  # 56px * 2
    "content_gap": 48,  # 24px * 2
    "photo_size": 280,  # 140px * 2
}

# Font sizes at 2x (will appear as half these values in final 1200x630)
FONTS = {
    "title": 92,  # 46px final
    "subtitle": 44,  # 22px final
    "label": 36,  # 18px final
    "value": 56,  # 28px final
    "small": 28,  # 14px final
}

# Component accent colors
COMPONENT_ACCENTS = {
    "vs_arena": COLORS["fuchsia"],
    "performance": COLORS["emerald"],
    "h2h": COLORS["fuchsia"],
    "comps": COLORS["cyan"],
}

# Fixed list lengths for determinism
LIST_LENGTHS = {
    "vs_arena": 6,
    "performance": 8,
    "h2h": 8,
    "comps": 6,
}

# Metric specs per category
# Each tuple: (metric_key, display_name, lower_is_better)
MetricSpec = Tuple[str, str, bool]

ANTHRO_METRICS: Tuple[MetricSpec, ...] = (
    ("wingspan_in", "Wingspan", False),
    ("standing_reach_in", "Standing Reach", False),
    ("height_w_shoes_in", "Height (Shoes)", False),
    ("height_wo_shoes_in", "Height (Barefoot)", False),
    ("weight_lb", "Weight", False),
    ("body_fat_pct", "Body Fat", True),
    ("hand_length_in", "Hand Length", False),
    ("hand_width_in", "Hand Width", False),
)

COMBINE_METRICS: Tuple[MetricSpec, ...] = (
    ("lane_agility_time_s", "Lane Agility", True),
    ("shuttle_run_s", "Shuttle Run", True),
    ("three_quarter_sprint_s", "3/4 Sprint", True),
    ("standing_vertical_in", "Standing Vert", False),
    ("max_vertical_in", "Max Vert", False),
    ("bench_press_reps", "Bench Reps", False),
)

SHOOTING_METRICS: Tuple[MetricSpec, ...] = (
    ("spot_up", "Spot-Up", False),
    ("off_dribble", "Off-Dribble", False),
    ("three_point_star", "3PT Star", False),
    ("midrange_star", "Mid-Range Star", False),
    ("three_point_side", "3PT Side", False),
    ("midrange_side", "Mid-Range Side", False),
    ("free_throw", "Free Throws", False),
)

METRIC_SPECS = {
    "anthropometrics": ANTHRO_METRICS,
    "combine": COMBINE_METRICS,
    "shooting": SHOOTING_METRICS,
}

# Comparison group labels for context line
# Note: "current_draft" is formatted dynamically with player's draft_year
COMPARISON_GROUP_LABELS = {
    "current_draft": "Compared to {year} Draft Class",  # Formatted dynamically
    "current_nba": "Compared to Current NBA Players",
    "all_time_draft": "Compared to All Historical Draft Prospects",
    "all_time_nba": "Compared to All Historical NBA Players",
}

# Metric group labels for context line
METRIC_GROUP_LABELS = {
    "anthropometrics": "Anthropometrics",
    "combine": "Athletic Performance",
    "shooting": "Shooting",
    "advanced": "Advanced Stats",
}

# Percentile tier thresholds
PERCENTILE_TIERS = {
    "elite": 90,
    "good": 70,
    "average": 40,
    "below": 0,
}


def get_percentile_tier(percentile: int) -> str:
    """Return tier name for a percentile value."""
    if percentile >= PERCENTILE_TIERS["elite"]:
        return "elite"
    if percentile >= PERCENTILE_TIERS["good"]:
        return "good"
    if percentile >= PERCENTILE_TIERS["average"]:
        return "average"
    return "below"


def get_tier_color(tier: str) -> str:
    """Return accent color for a tier."""
    return {
        "elite": COLORS["emerald"],
        "good": COLORS["cyan"],
        "average": COLORS["amber"],
        "below": COLORS["rose"],
        "unknown": COLORS["slate_400"],
    }.get(tier, COLORS["slate_400"])
