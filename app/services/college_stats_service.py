"""Scrape and ingest college basketball stats from Basketball-Reference.

Parses the college stats table embedded in BBRef player pages (inside an
HTML comment), computes per-game averages from season totals, and upserts
rows into ``player_college_stats``.

Designed to run as:
  - A cron stage (via ``run_college_stats_sweep``) picking up newly
    enriched players who lack authoritative stats.
  - A manual CLI backfill (via ``scripts/scrape_college_stats.py``)
    for the full player database.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup, Comment
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.player_college_stats import PlayerCollegeStats
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.players_master import PlayerMaster

logger = logging.getLogger(__name__)

_BBREF_BASE = "https://www.basketball-reference.com"
_USER_AGENT = "DraftGuru/1.0 (https://draftguru.dev; contact@draftguru.dev)"
_DEFAULT_THROTTLE = 3.0
_DEFAULT_CACHE_DIR = Path("scraper/cache/players")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class CollegeSeasonRow:
    """Parsed per-game stats for one college season."""

    season: str
    games: Optional[int] = None
    games_started: Optional[int] = None
    mpg: Optional[float] = None
    ppg: Optional[float] = None
    rpg: Optional[float] = None
    apg: Optional[float] = None
    spg: Optional[float] = None
    bpg: Optional[float] = None
    tov: Optional[float] = None
    pf: Optional[float] = None
    fg_pct: Optional[float] = None
    three_p_pct: Optional[float] = None
    three_pa: Optional[float] = None
    ft_pct: Optional[float] = None
    fta: Optional[float] = None


@dataclass
class SweepResult:
    """Summary of a college stats sweep run."""

    players_attempted: int = 0
    players_scraped: int = 0
    players_skipped: int = 0
    players_failed: int = 0
    seasons_upserted: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


def _safe_int(text: Optional[str]) -> Optional[int]:
    """Parse an integer from cell text, returning None on failure."""
    if not text or not text.strip():
        return None
    try:
        return int(text.strip())
    except (ValueError, TypeError):
        return None


def _safe_float(text: Optional[str]) -> Optional[float]:
    """Parse a float from cell text, returning None on failure."""
    if not text or not text.strip():
        return None
    try:
        return float(text.strip())
    except (ValueError, TypeError):
        return None


def _per_game(total: Optional[int], games: Optional[int]) -> Optional[float]:
    """Compute a per-game average, rounded to 1 decimal."""
    if total is None or games is None or games == 0:
        return None
    return round(total / games, 1)


def _pct_to_display(value: Optional[float]) -> Optional[float]:
    """Convert BBRef decimal percentage (.551) to display form (55.1)."""
    if value is None:
        return None
    return round(value * 100, 1)


def parse_college_stats_html(html: str) -> List[CollegeSeasonRow]:
    """Extract college season stats from a BBRef player page.

    The college stats table on BBRef is wrapped inside an HTML comment.
    This function finds that comment, parses the table, and returns one
    ``CollegeSeasonRow`` per season (skipping the Career footer row).

    Args:
        html: Full HTML content of a BBRef player page.

    Returns:
        List of parsed season rows (empty if no college stats found).
    """
    soup = BeautifulSoup(html, "html.parser")

    # The college stats table is inside an HTML comment within a div
    # whose id is "all_all_college_stats".
    wrapper = soup.find("div", id="all_all_college_stats")
    if not wrapper:
        return []

    # Find the comment containing the table
    table_html: Optional[str] = None
    for comment in wrapper.find_all(string=lambda t: isinstance(t, Comment)):
        if "all_college_stats" in comment:
            table_html = comment
            break

    if not table_html:
        return []

    # Parse the comment content as HTML
    table_soup = BeautifulSoup(table_html, "html.parser")
    table = table_soup.find("table", id="all_college_stats")
    if not table:
        return []

    tbody = table.find("tbody")
    if not tbody:
        return []

    rows: List[CollegeSeasonRow] = []
    for tr in tbody.find_all("tr"):
        # Skip header rows that sometimes appear in tbody
        if tr.get("class") and "thead" in tr.get("class", []):
            continue

        def cell(stat: str) -> Optional[str]:
            td = tr.find(["td", "th"], attrs={"data-stat": stat})
            if not td:
                return None
            return td.get_text(strip=True)

        season_text = cell("season")
        if not season_text:
            continue

        # Validate season format (e.g., "2016-17")
        if not re.match(r"^\d{4}-\d{2}$", season_text):
            continue

        games = _safe_int(cell("g"))

        # Totals for computing per-game averages
        stl_total = _safe_int(cell("stl"))
        blk_total = _safe_int(cell("blk"))
        tov_total = _safe_int(cell("tov"))
        pf_total = _safe_int(cell("pf"))
        fg3a_total = _safe_int(cell("fg3a"))
        fta_total = _safe_int(cell("fta"))

        # Per-game values provided by BBRef
        mpg = _safe_float(cell("mp_per_g"))
        ppg = _safe_float(cell("pts_per_g"))
        rpg = _safe_float(cell("trb_per_g"))
        apg = _safe_float(cell("ast_per_g"))

        # Shooting percentages (BBRef stores as decimals like .551)
        fg_pct = _pct_to_display(_safe_float(cell("fg_pct")))
        three_p_pct = _pct_to_display(_safe_float(cell("fg3_pct")))
        ft_pct = _pct_to_display(_safe_float(cell("ft_pct")))

        rows.append(
            CollegeSeasonRow(
                season=season_text,
                games=games,
                games_started=None,  # Not in BBRef college stats table
                mpg=mpg,
                ppg=ppg,
                rpg=rpg,
                apg=apg,
                spg=_per_game(stl_total, games),
                bpg=_per_game(blk_total, games),
                tov=_per_game(tov_total, games),
                pf=_per_game(pf_total, games),
                fg_pct=fg_pct,
                three_p_pct=three_p_pct,
                three_pa=_per_game(fg3a_total, games),
                ft_pct=ft_pct,
                fta=_per_game(fta_total, games),
            )
        )

    return rows


# ---------------------------------------------------------------------------
# HTTP fetching with caching
# ---------------------------------------------------------------------------


def _bbref_url(slug: str) -> str:
    """Build a BBRef player page URL from a slug like 'balllo01'."""
    letter = slug[0]
    return f"{_BBREF_BASE}/players/{letter}/{slug}.html"


def fetch_player_html(
    slug: str,
    cache_dir: Path = _DEFAULT_CACHE_DIR,
    refresh: bool = False,
    throttle: float = _DEFAULT_THROTTLE,
) -> Optional[str]:
    """Fetch a BBRef player page, using a local file cache.

    Args:
        slug: BBRef player slug (e.g. 'balllo01').
        cache_dir: Directory for cached HTML files.
        refresh: If True, re-download even when cached.
        throttle: Seconds to sleep after a live HTTP request.

    Returns:
        HTML content string, or None on failure.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{slug}.html"

    if not refresh and cache_path.exists():
        logger.debug("Cache hit for %s", slug)
        return cache_path.read_text(encoding="utf-8", errors="ignore")

    url = _bbref_url(slug)
    logger.info("Fetching %s", url)

    try:
        with httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=30.0,
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
    except Exception:
        logger.exception("Failed to fetch %s", url)
        # Fall back to cache if available
        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8", errors="ignore")
        return None

    html = resp.text
    cache_path.write_text(html, encoding="utf-8")

    if throttle > 0:
        time.sleep(throttle)

    return html


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------


async def upsert_college_stats(
    db: AsyncSession,
    player_id: int,
    season_rows: List[CollegeSeasonRow],
) -> int:
    """Upsert parsed college stats into the database.

    Args:
        db: Active database session (caller manages transaction).
        player_id: The player's ID in players_master.
        season_rows: Parsed season data to persist.

    Returns:
        Number of rows upserted.
    """
    if not season_rows:
        return 0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    count = 0

    for row in season_rows:
        values = {
            "player_id": player_id,
            "season": row.season,
            "games": row.games,
            "games_started": row.games_started,
            "mpg": row.mpg,
            "ppg": row.ppg,
            "rpg": row.rpg,
            "apg": row.apg,
            "spg": row.spg,
            "bpg": row.bpg,
            "tov": row.tov,
            "pf": row.pf,
            "fg_pct": row.fg_pct,
            "three_p_pct": row.three_p_pct,
            "three_pa": row.three_pa,
            "ft_pct": row.ft_pct,
            "fta": row.fta,
            "source": "sports_reference",
            "updated_at": now,
        }

        stmt = insert(PlayerCollegeStats).values(**values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_college_stats_player_season",
            set_={k: v for k, v in values.items() if k not in ("player_id", "season")},
        )
        await db.execute(stmt)
        count += 1

    return count


async def _find_eligible_players(
    db: AsyncSession,
    *,
    player_id: Optional[int] = None,
    only_missing: bool = False,
) -> list[tuple[int, str, str]]:
    """Find players with a school and BBRef external ID.

    Args:
        db: Active database session.
        player_id: If set, restrict to this single player.
        only_missing: If True, exclude players who already have
            ``source='sports_reference'`` college stats rows.

    Returns:
        List of (player_id, display_name, bbr_slug) tuples.
    """
    stmt = (
        select(  # type: ignore[call-overload]
            PlayerMaster.id,
            PlayerMaster.display_name,
            PlayerExternalId.external_id,
        )
        .join(
            PlayerExternalId,
            PlayerExternalId.player_id == PlayerMaster.id,  # type: ignore[arg-type]
        )
        .where(PlayerExternalId.system == "bbr")
        .where(PlayerMaster.school.isnot(None))  # type: ignore[union-attr]
    )

    if player_id is not None:
        stmt = stmt.where(PlayerMaster.id == player_id)  # type: ignore[arg-type]

    if only_missing:
        # Exclude players who already have sports_reference stats
        existing_subq = (
            select(PlayerCollegeStats.player_id)  # type: ignore[call-overload]
            .where(PlayerCollegeStats.source == "sports_reference")
            .distinct()
            .subquery()
        )
        stmt = stmt.where(
            PlayerMaster.id.notin_(  # type: ignore[union-attr]
                select(existing_subq.c.player_id)  # type: ignore[call-overload]
            )
        )

    stmt = stmt.order_by(PlayerMaster.id)  # type: ignore[arg-type]

    result = await db.execute(stmt)
    return [(row[0], row[1] or "", row[2]) for row in result.all()]


# ---------------------------------------------------------------------------
# Sweep entry point
# ---------------------------------------------------------------------------


async def run_college_stats_sweep(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    limit: Optional[int] = None,
    player_id: Optional[int] = None,
    dry_run: bool = False,
    refresh: bool = False,
    throttle: float = _DEFAULT_THROTTLE,
    cache_dir: Path = _DEFAULT_CACHE_DIR,
    only_missing: bool = False,
) -> SweepResult:
    """Scrape BBRef college stats for eligible players and upsert to DB.

    Args:
        session_factory: Async session factory for DB access.
        limit: Max number of players to process (None = all).
        player_id: Restrict to a single player ID.
        dry_run: Parse and log but skip DB writes.
        refresh: Re-fetch cached HTML pages.
        throttle: Seconds between live HTTP requests.
        cache_dir: Directory for cached HTML files.
        only_missing: Only process players without existing
            ``source='sports_reference'`` stats.

    Returns:
        Summary of the sweep run.
    """
    result = SweepResult()

    # Find eligible players
    async with session_factory() as db:
        players = await _find_eligible_players(
            db, player_id=player_id, only_missing=only_missing
        )

    if limit is not None:
        players = players[:limit]

    if not players:
        logger.info("No eligible players found for college stats scraping")
        return result

    logger.info("Found %d eligible players for college stats", len(players))

    for pid, display_name, bbr_slug in players:
        result.players_attempted += 1

        try:
            # Fetch and parse (synchronous HTTP, no DB transaction held)
            html = fetch_player_html(
                bbr_slug,
                cache_dir=cache_dir,
                refresh=refresh,
                throttle=throttle,
            )
            if not html:
                logger.warning(
                    "No HTML for %s (slug=%s), skipping", display_name, bbr_slug
                )
                result.players_skipped += 1
                continue

            season_rows = parse_college_stats_html(html)
            if not season_rows:
                logger.debug(
                    "No college stats found for %s (slug=%s)", display_name, bbr_slug
                )
                result.players_skipped += 1
                continue

            if dry_run:
                for row in season_rows:
                    logger.info(
                        "[dry-run] %s %s: %s games, %.1f ppg, %.1f rpg, %.1f apg",
                        display_name,
                        row.season,
                        row.games or 0,
                        row.ppg or 0,
                        row.rpg or 0,
                        row.apg or 0,
                    )
                result.players_scraped += 1
                result.seasons_upserted += len(season_rows)
                continue

            # Upsert in a short transaction
            async with session_factory() as db:
                async with db.begin():
                    count = await upsert_college_stats(db, pid, season_rows)
                    result.seasons_upserted += count

            result.players_scraped += 1
            logger.info(
                "Upserted %d season(s) for %s (slug=%s)",
                len(season_rows),
                display_name,
                bbr_slug,
            )

        except Exception as exc:
            result.players_failed += 1
            error_msg = f"Failed to process {display_name} (slug={bbr_slug}): {exc}"
            result.errors.append(error_msg)
            logger.error(error_msg, exc_info=True)

    logger.info(
        "College stats sweep complete: %d attempted, %d scraped, "
        "%d skipped, %d failed, %d seasons upserted",
        result.players_attempted,
        result.players_scraped,
        result.players_skipped,
        result.players_failed,
        result.seasons_upserted,
    )

    return result
