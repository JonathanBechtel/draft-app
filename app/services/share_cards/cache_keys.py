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
    - sorted player_ids (for symmetry in vs/h2h)
    - normalized context (sorted keys)

    Args:
        component: Component type (vs_arena, performance, h2h, comps)
        player_ids: List of player IDs involved
        context: Export context (comparison_group, same_position, metric_group)

    Returns:
        S3 key path for the export image
    """
    # Sort player IDs for symmetric comparisons (A vs B == B vs A)
    sorted_ids = sorted(player_ids)

    # Normalize context to JSON with sorted keys
    normalized_context = json.dumps(context, sort_keys=True)

    # Build hash input
    hash_input = f"{TEMPLATE_VERSION}|{sorted_ids}|{normalized_context}"

    # Generate SHA256 hash, take first 16 chars
    hash_digest = hashlib.sha256(hash_input.encode()).hexdigest()[:16]

    # Use players/exports/ path to match S3 bucket policy that allows players/* prefix
    return f"players/exports/{component}/{hash_digest}.png"


def generate_filename(
    component: str,
    player_names: list[str],
) -> str:
    """Generate human-readable download filename.

    Args:
        component: Component type
        player_names: List of player display names

    Returns:
        Filename like "cooper-flagg-vs-dylan-harper.png"
    """
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
) -> str:
    """Generate display title for the export.

    Args:
        component: Component type
        player_names: List of player display names

    Returns:
        Title like "Cooper Flagg vs Dylan Harper"
    """
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
