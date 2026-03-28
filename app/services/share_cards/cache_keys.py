"""Content-addressed cache key generation for share card exports."""

import hashlib
import json
import re
from typing import Any

from app.services.share_cards.constants import TEMPLATE_VERSION


def generate_cache_key(
    component: str,
    player_ids: list[int],
    context: dict[str, Any],
) -> str:
    """Generate content-addressed cache key for export images.

    Key format: exports/{component}/{hash}.png

    Hash inputs:
    - template_version (bumped when templates change)
    - ordered player_ids for vs/h2h (A/B layout matters)
    - sorted player_ids for other components (determinism)
    - normalized context (sorted keys)

    Args:
        component: Component type (vs_arena, performance, h2h, comps)
        player_ids: List of player IDs involved
        context: Export context (comparison_group, same_position, metric_group)

    Returns:
        S3 key path for the export image
    """
    if component in {"vs_arena", "h2h"}:
        # Preserve A/B ordering since the rendered layout is directional.
        ids_for_key = player_ids
    else:
        # Deterministic for any unordered/single-player components.
        ids_for_key = sorted(player_ids)

    # Normalize context to JSON with sorted keys
    normalized_context = json.dumps(context, sort_keys=True)

    # Build hash input
    hash_input = f"{TEMPLATE_VERSION}|{ids_for_key}|{normalized_context}"

    # Generate SHA256 hash, take first 16 chars
    hash_digest = hashlib.sha256(hash_input.encode()).hexdigest()[:16]

    # Use players/exports/ path to match S3 bucket policy that allows players/* prefix
    return f"players/exports/{component}/{hash_digest}.png"


def generate_filename(
    component: str,
    player_names: list[str],
    context: dict[str, Any] | None = None,
) -> str:
    """Generate human-readable download filename.

    Args:
        component: Component type
        player_names: List of player display names
        context: Optional export context for stats-based cards

    Returns:
        Filename like "cooper-flagg-vs-dylan-harper.png"
    """
    if component == "metric_leaders":
        metric_key = (context or {}).get("metric_key", "metric")
        slug = _slugify(metric_key.replace("_", " "))
        return f"{slug}-leaders.png"
    elif component == "draft_year":
        year = (context or {}).get("year", "draft")
        category = (context or {}).get("category", "combine")
        return f"{year}-combine-{category}.png"

    slugified = [_slugify(name) for name in player_names]

    if component in ("vs_arena", "h2h"):
        if len(slugified) >= 2:
            return f"{slugified[0]}-vs-{slugified[1]}.png"
        return f"{slugified[0]}-comparison.png"
    else:
        return f"{slugified[0]}-{component.replace('_', '-')}.png"


def generate_title(
    component: str,
    player_names: list[str],
    context: dict[str, Any] | None = None,
) -> str:
    """Generate display title for the export.

    Args:
        component: Component type
        player_names: List of player display names
        context: Optional export context for stats-based cards

    Returns:
        Title like "Cooper Flagg vs Dylan Harper"
    """
    if component == "metric_leaders":
        metric_display = (context or {}).get("metric_display_name", "Metric")
        return f"Top {metric_display} — Combine Leaders"
    elif component == "draft_year":
        year = (context or {}).get("year", "")
        category_labels = {
            "anthro": "Measurements",
            "athletic": "Athletic Testing",
            "shooting": "Shooting",
        }
        category = (context or {}).get("category", "combine")
        cat_label = category_labels.get(category, "Combine")
        return f"{year} Combine — {cat_label}"

    if component == "vs_arena":
        if len(player_names) >= 2:
            return f"{player_names[0]} vs {player_names[1]}"
        return player_names[0]
    elif component == "h2h":
        if len(player_names) >= 2:
            return f"{player_names[0]} vs {player_names[1]}"
        return player_names[0]
    elif component == "performance":
        return f"{player_names[0]} — Performance"
    elif component == "comps":
        return f"{player_names[0]} — Comparisons"
    else:
        return player_names[0]


def _slugify(name: str) -> str:
    """Convert name to URL-safe slug."""
    # Lowercase and replace spaces/special chars with hyphens
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug
