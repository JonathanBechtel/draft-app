"""Stub player auto-enrichment pipeline.

Fills in biographical details, college stats, RSCI rank, and reference
images for players created as stubs during news tagging.  Designed to
run as the last step in the cron cycle.

Three stages:
  1. Bio + Stats  — Gemini Flash with Google Search grounding
  2. Reference Image — Wikimedia Commons API
  3. Player Portrait — existing image generation pipeline (future)
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import httpx
from google import genai
from google.genai import types
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.fields import CohortType
from app.schemas.image_snapshots import PlayerImageSnapshot
from app.schemas.player_college_stats import PlayerCollegeStats
from app.schemas.players_master import PlayerMaster
from app.services.image_generation import image_generation_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class EnrichmentResult:
    """Summary of an enrichment run."""

    players_attempted: int = 0
    players_enriched: int = 0
    players_failed: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Gemini prompt for bio + stats extraction
# ---------------------------------------------------------------------------

BIO_STATS_PROMPT = """You are a sports data researcher. Given an NBA draft prospect's name, find their biographical details and most recent college basketball season statistics.

Return valid JSON with the following structure. Use null for any field you cannot confidently determine.

{
  "confidence": "high" | "medium" | "low",
  "birthdate": "YYYY-MM-DD" or null,
  "birth_city": "city name" or null,
  "birth_state_province": "state/province" or null,
  "birth_country": "country" or null,
  "school": "college/university name" or null,
  "high_school": "high school name" or null,
  "height_inches": integer or null,
  "weight_lbs": integer or null,
  "position": "PG" | "SG" | "SF" | "PF" | "C" or null,
  "shoots": "Right" | "Left" or null,
  "rsci_rank": integer or null (Rivals/RSCI composite recruiting rank, if it exists),
  "draft_year": integer or null (expected or actual NBA draft year),
  "likeness_description": "brief physical description for an illustrator" or null,
  "season": "YYYY-YY" format (e.g. "2024-25") or null,
  "stats": {
    "games": integer or null,
    "games_started": integer or null,
    "mpg": float or null,
    "ppg": float or null,
    "rpg": float or null,
    "apg": float or null,
    "spg": float or null,
    "bpg": float or null,
    "fg_pct": float or null (as percentage, e.g. 45.2),
    "three_p_pct": float or null,
    "three_pa": float or null (three-point attempts per game),
    "ft_pct": float or null,
    "fta": float or null (free throw attempts per game),
    "tov": float or null (turnovers per game),
    "pf": float or null (personal fouls per game)
  }
}

Important:
- Only return data you can verify from search results
- Set confidence to "low" if you're unsure about most fields
- For height, convert to total inches (e.g. 6'5" = 77)
- Stats should be per-game averages for the most recent college season
- RSCI rank is only for players who were highly rated high school recruits
- Respond with valid JSON only, no markdown formatting"""


# ---------------------------------------------------------------------------
# Stage 1: Bio + Stats via Gemini with Google Search grounding
# ---------------------------------------------------------------------------


async def _fetch_bio_and_stats(
    client: genai.Client, player_name: str
) -> Optional[dict]:
    """Call Gemini Flash with Google Search grounding to get bio + stats.

    Args:
        client: Initialized Gemini client.
        player_name: Display name of the player.

    Returns:
        Parsed dict from Gemini response, or None on failure.
    """
    user_prompt = (
        f"Find biographical details and college basketball statistics for "
        f"{player_name}, a current or recent NBA draft prospect."
    )

    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_prompt)],
            ),
            config=types.GenerateContentConfig(
                system_instruction=[types.Part.from_text(text=BIO_STATS_PROMPT)],
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.2,
            ),
        )

        response_text = response.text if response.text else ""
        logger.debug("Gemini response for %s: %s", player_name, response_text)

        return _parse_json_response(response_text)

    except Exception:
        logger.exception("Gemini bio+stats fetch failed for %s", player_name)
        return None


def _parse_json_response(text: str) -> Optional[dict]:
    """Parse JSON from Gemini response, stripping markdown fences."""
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
        logger.warning("Gemini returned non-dict JSON: %s", type(data))
        return None
    except json.JSONDecodeError:
        logger.warning("Failed to parse Gemini JSON: %.200s", cleaned)
        return None


# ---------------------------------------------------------------------------
# Stage 2: Reference image via Wikimedia Commons
# ---------------------------------------------------------------------------

_WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
_WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
_ACCEPTED_LICENSES = {
    "cc-by-sa-4.0",
    "cc-by-sa-3.0",
    "cc-by-sa-2.0",
    "cc-by-4.0",
    "cc-by-3.0",
    "cc-by-2.0",
    "cc0",
    "pd",
    "public domain",
}
_WIKIMEDIA_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
_WIKIMEDIA_HEADERS = {
    "User-Agent": "DraftGuru/1.0 (https://draftguru.dev; contact@draftguru.dev)",
}


async def _find_reference_image(player_name: str) -> Optional[str]:
    """Search Wikimedia Commons and Wikipedia for a CC-licensed player photo.

    Args:
        player_name: Display name of the player.

    Returns:
        Direct URL to a CC-licensed image, or None.
    """
    async with httpx.AsyncClient(
        timeout=_WIKIMEDIA_TIMEOUT, headers=_WIKIMEDIA_HEADERS
    ) as http:
        # Try Wikimedia Commons first
        url = await _search_commons(http, player_name)
        if url:
            return url

        # Fallback: Wikipedia article infobox image
        url = await _search_wikipedia_image(http, player_name)
        if url:
            return url

    return None


async def _search_commons(http: httpx.AsyncClient, player_name: str) -> Optional[str]:
    """Search Wikimedia Commons for a CC-licensed photo."""
    params = {
        "action": "query",
        "list": "search",
        "srsearch": f'"{player_name}" basketball',
        "srnamespace": "6",  # File namespace
        "srlimit": "5",
        "format": "json",
    }

    try:
        resp = await http.get(_WIKIMEDIA_API, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("Wikimedia Commons search failed for %s", player_name)
        return None

    results = data.get("query", {}).get("search", [])
    for item in results:
        title = item.get("title", "")
        if not title:
            continue

        image_url = await _get_commons_file_url(http, title)
        if image_url:
            return image_url

    return None


async def _get_commons_file_url(
    http: httpx.AsyncClient, file_title: str
) -> Optional[str]:
    """Fetch file info from Wikimedia Commons and verify license."""
    params = {
        "action": "query",
        "titles": file_title,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
        "format": "json",
    }

    try:
        resp = await http.get(_WIKIMEDIA_API, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.debug("Failed to fetch file info for %s", file_title)
        return None

    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        image_info = page.get("imageinfo", [])
        if not image_info:
            continue

        info = image_info[0]
        url = info.get("url")
        metadata = info.get("extmetadata", {})
        license_short = metadata.get("LicenseShortName", {}).get("value", "").lower()

        if any(lic in license_short for lic in _ACCEPTED_LICENSES):
            logger.info(
                "Found CC image for %s: %s (license: %s)",
                file_title,
                url,
                license_short,
            )
            return url  # type: ignore[return-value]

    return None


async def _search_wikipedia_image(
    http: httpx.AsyncClient, player_name: str
) -> Optional[str]:
    """Fallback: get the main image from a player's Wikipedia article."""
    params = {
        "action": "query",
        "titles": player_name,
        "prop": "pageimages",
        "piprop": "original",
        "format": "json",
    }

    try:
        resp = await http.get(_WIKIPEDIA_API, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.debug("Wikipedia image search failed for %s", player_name)
        return None

    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        original = page.get("original", {})
        source = original.get("source")
        if source:
            logger.info("Found Wikipedia image for %s: %s", player_name, source)
            return source

    return None


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _parse_date(value: Optional[str]) -> Optional[date]:
    """Parse a YYYY-MM-DD string into a date, returning None on failure."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


async def _apply_bio_data(db: AsyncSession, player: PlayerMaster, data: dict) -> bool:
    """Update PlayerMaster fields from Gemini bio response.

    Only updates fields that are currently empty on the player record.

    Returns:
        True if at least one field was updated.
    """
    confidence = data.get("confidence", "low")
    if confidence == "low":
        logger.info("Skipping bio for %s: low confidence", player.display_name)
        return False

    field_map = {
        "birthdate": ("birthdate", _parse_date),
        "birth_city": ("birth_city", None),
        "birth_state_province": ("birth_state_province", None),
        "birth_country": ("birth_country", None),
        "school": ("school", None),
        "high_school": ("high_school", None),
        "shoots": ("shoots", None),
        "draft_year": ("draft_year", None),
        "rsci_rank": ("rsci_rank", None),
    }

    updated_fields: list[str] = []
    for json_key, (model_field, transform) in field_map.items():
        value = data.get(json_key)
        if value is None:
            continue
        # Only fill in empty fields
        if getattr(player, model_field) is not None:
            continue
        if transform:
            value = transform(value)
            if value is None:
                continue
        setattr(player, model_field, value)
        updated_fields.append(model_field)

    # Height/weight go to PlayerMaster for now (PlayerStatus is separate)
    # We'll store them as bio-level data since these are listed/reported values
    # not combine-measured values

    if updated_fields:
        player.bio_source = "ai_generated"
        logger.info("Updated bio for %s: %s", player.display_name, updated_fields)
        return True

    return False


async def _apply_stats_data(db: AsyncSession, player: PlayerMaster, data: dict) -> bool:
    """Upsert college stats from Gemini response.

    Returns:
        True if stats were persisted.
    """
    stats = data.get("stats")
    season = data.get("season")
    if not stats or not season:
        logger.debug("No stats/season data for %s", player.display_name)
        return False

    player_id = player.id
    if player_id is None:
        return False

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    values = {
        "player_id": player_id,
        "season": season,
        "games": stats.get("games"),
        "games_started": stats.get("games_started"),
        "mpg": stats.get("mpg"),
        "ppg": stats.get("ppg"),
        "rpg": stats.get("rpg"),
        "apg": stats.get("apg"),
        "spg": stats.get("spg"),
        "bpg": stats.get("bpg"),
        "fg_pct": stats.get("fg_pct"),
        "three_p_pct": stats.get("three_p_pct"),
        "three_pa": stats.get("three_pa"),
        "ft_pct": stats.get("ft_pct"),
        "fta": stats.get("fta"),
        "tov": stats.get("tov"),
        "pf": stats.get("pf"),
        "source": "ai_generated",
        "updated_at": now,
    }

    stmt = insert(PlayerCollegeStats).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_college_stats_player_season",
        set_={k: v for k, v in values.items() if k not in ("player_id", "season")},
    )
    await db.execute(stmt)
    logger.info("Upserted college stats for %s (%s)", player.display_name, season)
    return True


# ---------------------------------------------------------------------------
# Stage 3: Player portrait generation
# ---------------------------------------------------------------------------


async def _generate_portrait(db: AsyncSession, player: PlayerMaster) -> None:
    """Generate a DraftGuru-style portrait using the existing image pipeline.

    Creates a one-off snapshot record and calls the synchronous generation
    path.  If the player has a reference_image_url, it will be used for
    likeness matching.

    Args:
        db: Active database session (caller manages the transaction).
        player: Enriched player record.
    """
    player_id = player.id
    if player_id is None:
        return

    system_prompt = image_generation_service.get_system_prompt("default")
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    snapshot = PlayerImageSnapshot(
        run_key=f"enrichment_{player_id}",
        version=1,
        is_current=False,
        style="default",
        cohort=CohortType.global_scope,
        population_size=1,
        image_size=settings.image_gen_size,
        system_prompt=system_prompt,
        system_prompt_version="default",
        notes=f"Auto-generated after enrichment for {player.display_name}",
        generated_at=now,
    )
    db.add(snapshot)
    await db.flush()

    asset = await image_generation_service.generate_for_player(
        db=db,
        player=player,
        snapshot=snapshot,
        style="default",
        fetch_likeness=bool(player.reference_image_url),
    )

    if asset.error_message:
        logger.warning(
            "Portrait generation error for %s: %s",
            player.display_name,
            asset.error_message,
        )
    else:
        logger.info(
            "Generated portrait for %s: %s",
            player.display_name,
            asset.public_url,
        )


# ---------------------------------------------------------------------------
# Single-player enrichment orchestrator
# ---------------------------------------------------------------------------


async def enrich_player(
    db: AsyncSession,
    player: PlayerMaster,
    client: genai.Client,
) -> bool:
    """Run the enrichment pipeline for a single stub player.

    Stages 1 (bio+stats) and 2 (reference image) run concurrently.
    Stage 3 (image generation) is deferred to future work.

    Args:
        db: Active database session.
        player: The stub PlayerMaster record.
        client: Initialized Gemini client.

    Returns:
        True if any data was persisted, False otherwise.
    """
    player_name = player.display_name or ""
    if not player_name:
        logger.warning("Player id=%s has no display_name, skipping", player.id)
        return False

    logger.info("Enriching stub player: %s (id=%s)", player_name, player.id)

    # Run Stage 1 and Stage 2 concurrently
    bio_task = asyncio.create_task(_fetch_bio_and_stats(client, player_name))
    image_task = asyncio.create_task(_find_reference_image(player_name))

    bio_data, reference_image_url = await asyncio.gather(
        bio_task, image_task, return_exceptions=True
    )

    enriched = False

    # Apply Stage 1: Bio + Stats
    if isinstance(bio_data, dict):
        bio_updated = await _apply_bio_data(db, player, bio_data)
        stats_updated = await _apply_stats_data(db, player, bio_data)
        if bio_updated or stats_updated:
            enriched = True
    elif isinstance(bio_data, Exception):
        logger.error("Bio fetch failed for %s: %s", player_name, bio_data)

    # Apply Stage 2: Reference image
    if isinstance(reference_image_url, str) and reference_image_url:
        if not player.reference_image_url:
            player.reference_image_url = reference_image_url
            logger.info("Set reference image for %s", player_name)
            enriched = True
    elif isinstance(reference_image_url, Exception):
        logger.error("Image search failed for %s: %s", player_name, reference_image_url)

    # Stage 3: Generate player portrait (requires reference image or likeness)
    if enriched:
        try:
            await _generate_portrait(db, player)
        except Exception:
            logger.exception("Portrait generation failed for %s", player_name)

    # Stamp enrichment attempt regardless of outcome
    player.enrichment_attempted_at = datetime.now(timezone.utc).replace(tzinfo=None)

    return enriched


# ---------------------------------------------------------------------------
# Cron entry point
# ---------------------------------------------------------------------------


async def run_enrichment_sweep(
    session_factory: async_sessionmaker[AsyncSession],
) -> EnrichmentResult:
    """Find unenriched stub players and run the enrichment pipeline.

    Intended to be called as the last step in the cron runner.  Uses its
    own short-lived sessions so transaction control stays in cron code.

    Args:
        session_factory: Async session factory for DB access.

    Returns:
        Summary of the enrichment run.
    """
    result = EnrichmentResult()

    if not settings.gemini_api_key:
        logger.warning("GEMINI_API_KEY not configured, skipping enrichment")
        return result

    client = genai.Client(api_key=settings.gemini_api_key)

    # Find stub players that haven't been enriched yet
    async with session_factory() as db:
        stmt = (
            select(PlayerMaster)  # type: ignore[call-overload]
            .where(PlayerMaster.is_stub == True)  # type: ignore[arg-type]  # noqa: E712
            .where(PlayerMaster.enrichment_attempted_at.is_(None))  # type: ignore[union-attr]
            .order_by(PlayerMaster.id)  # type: ignore[arg-type]
        )
        rows = await db.execute(stmt)
        # Collect IDs+names so we can re-fetch in per-player sessions
        player_refs = [(p.id, p.display_name) for p in rows.scalars().all()]

    if not player_refs:
        logger.info("No unenriched stub players found")
        return result

    logger.info("Found %d stub players to enrich", len(player_refs))

    for player_id, display_name in player_refs:
        result.players_attempted += 1
        try:
            async with session_factory() as db:
                async with db.begin():
                    player = await db.get(PlayerMaster, player_id)
                    if player is None:
                        continue
                    enriched = await enrich_player(db, player, client)
                    if enriched:
                        result.players_enriched += 1
        except Exception as exc:
            result.players_failed += 1
            error_msg = f"Failed to enrich {display_name}: {exc}"
            result.errors.append(error_msg)
            logger.error(error_msg, exc_info=True)
            # Stamp enrichment_attempted_at even on failure to prevent retries
            try:
                async with session_factory() as db:
                    async with db.begin():
                        player = await db.get(PlayerMaster, player_id)
                        if player is not None:
                            player.enrichment_attempted_at = datetime.now(
                                timezone.utc
                            ).replace(tzinfo=None)
            except Exception:
                logger.exception(
                    "Failed to stamp enrichment_attempted_at for %s",
                    display_name,
                )

    return result
