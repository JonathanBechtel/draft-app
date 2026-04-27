"""Enrich Top 100 college prospects from Sports-Reference CBB pages.

The script resolves current NCAA prospects to College Basketball at
Sports-Reference player pages, parses current per-game stats, and updates dev
identity/status fields with clear provenance. It skips professional and
international affiliations for separate review.

Usage:
    conda run -n draftguru python scripts/top100_cbb_enrich.py --dry-run
    conda run -n draftguru python scripts/top100_cbb_enrich.py --execute
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Comment
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.canonical_resolution_service import (  # noqa: E402
    load_college_school_names,
    load_school_mapping,
    normalize_player_name,
    resolve_affiliation,
)
from app.services.player_mention_service import parse_player_name  # noqa: E402
from scripts.top100_audit import resolve_rows  # noqa: E402
from scripts.top100_refresh import OUTPUT_DIR, TOP100_ROWS, _prepare_connection  # noqa: E402


CBB_BASE_URL = "https://www.sports-reference.com"
CBB_SOURCE = "sports_reference_cbb"
CBB_CACHE_DIR = Path("scraper/cache/cbb")
CURRENT_SEASON = "2025-26"
USER_AGENT = "DraftGuru/1.0 (https://draftguru.app; contact@draftguru.app)"


@dataclass(frozen=True, slots=True)
class CbbStatsRow:
    """Parsed current-season per-game stats from a CBB player page."""

    season: str
    games: int | None = None
    games_started: int | None = None
    mpg: float | None = None
    ppg: float | None = None
    rpg: float | None = None
    apg: float | None = None
    spg: float | None = None
    bpg: float | None = None
    tov: float | None = None
    pf: float | None = None
    fg_pct: float | None = None
    three_p_pct: float | None = None
    three_pa: float | None = None
    ft_pct: float | None = None
    fta: float | None = None


@dataclass(frozen=True, slots=True)
class CbbProfile:
    """Resolved CBB page metadata and stats."""

    source_url: str
    cbb_slug: str
    display_name: str
    school: str
    position: str
    height_in: int | None
    weight_lb: int | None
    high_school: str
    hometown: str
    rsci_rank: int | None
    stats: CbbStatsRow | None


@dataclass(frozen=True, slots=True)
class ReviewRow:
    """One enrichment review row."""

    source_rank: int
    source_name: str
    player_id: int | None
    raw_affiliation: str
    canonical_affiliation: str
    status: str
    source_url: str = ""
    cbb_slug: str = ""
    db_action: str = ""
    reason: str = ""
    season: str = ""
    games: int | None = None
    ppg: float | None = None
    rpg: float | None = None
    apg: float | None = None
    position: str = ""
    height_in: int | None = None
    weight_lb: int | None = None
    high_school: str = ""
    rsci_rank: int | None = None


def _cache_path_for_url(url: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", url.replace(CBB_BASE_URL, "").strip("/"))
    return CBB_CACHE_DIR / f"{safe or 'index'}.html"


def _fetch(url: str, *, refresh: bool, throttle: float) -> str | None:
    """Fetch URL with a local cache."""
    CBB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _cache_path_for_url(url)
    if cache_path.exists() and not refresh:
        return cache_path.read_text(encoding="utf-8", errors="ignore")

    try:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
            follow_redirects=True,
        ) as client:
            response = client.get(url)
            response.raise_for_status()
    except Exception:
        return None

    cache_path.write_text(response.text, encoding="utf-8")
    if throttle > 0:
        time.sleep(throttle)
    return response.text


def _safe_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(float(value.strip()))
    except ValueError:
        return None


def _safe_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return round(float(value.strip()), 3)
    except ValueError:
        return None


def _pct(value: str | None) -> float | None:
    parsed = _safe_float(value)
    if parsed is None:
        return None
    return round(parsed * 100, 1) if parsed <= 1 else parsed


def _parse_height_in(text_value: str) -> int | None:
    match = re.search(r"(\d+)-(\d+)", text_value)
    if not match:
        return None
    return int(match.group(1)) * 12 + int(match.group(2))


def _parse_weight_lb(text_value: str) -> int | None:
    match = re.search(r"(\d+)lb", text_value)
    if not match:
        return None
    return int(match.group(1))


def _table_soup(soup: BeautifulSoup, table_id: str) -> BeautifulSoup | None:
    table = soup.find("table", id=table_id)
    if table:
        return BeautifulSoup(str(table), "html.parser")
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        if table_id in comment:
            parsed = BeautifulSoup(str(comment), "html.parser")
            table = parsed.find("table", id=table_id)
            if table:
                return BeautifulSoup(str(table), "html.parser")
    return None


def _cell(row: Any, stat: str) -> str:
    node = row.find(["td", "th"], attrs={"data-stat": stat})
    return node.get_text(strip=True) if node else ""


def _parse_stats(soup: BeautifulSoup) -> CbbStatsRow | None:
    table = _table_soup(soup, "players_per_game")
    if table is None:
        table = _table_soup(soup, "per_game")
    if table is None:
        return None

    rows = []
    for tr in table.find_all("tr"):
        season = _cell(tr, "year_id")
        if re.match(r"^\d{4}-\d{2}$", season):
            rows.append(tr)
    if not rows:
        return None

    current = next(
        (_row for _row in rows if _cell(_row, "season") == CURRENT_SEASON), rows[-1]
    )
    return CbbStatsRow(
        season=_cell(current, "year_id"),
        games=_safe_int(_cell(current, "games")),
        games_started=_safe_int(_cell(current, "games_started")),
        mpg=_safe_float(_cell(current, "mp_per_g")),
        ppg=_safe_float(_cell(current, "pts_per_g")),
        rpg=_safe_float(_cell(current, "trb_per_g")),
        apg=_safe_float(_cell(current, "ast_per_g")),
        spg=_safe_float(_cell(current, "stl_per_g")),
        bpg=_safe_float(_cell(current, "blk_per_g")),
        tov=_safe_float(_cell(current, "tov_per_g")),
        pf=_safe_float(_cell(current, "pf_per_g")),
        fg_pct=_pct(_cell(current, "fg_pct")),
        three_p_pct=_pct(_cell(current, "fg3_pct")),
        three_pa=_safe_float(_cell(current, "fg3a_per_g")),
        ft_pct=_pct(_cell(current, "ft_pct")),
        fta=_safe_float(_cell(current, "fta_per_g")),
    )


def _parse_profile(url: str, html: str) -> CbbProfile | None:
    soup = BeautifulSoup(html, "html.parser")
    name = soup.find("h1")
    if not name:
        return None

    meta_text = soup.get_text(" ", strip=True)
    school = ""
    school_match = re.search(r"School:\s*([^()]+)\s*\(Men\)", meta_text)
    if school_match:
        school = school_match.group(1).strip()

    high_school = ""
    high_school_match = re.search(
        r"High School:\s*([^•]+?)\s*(?:School:|RSCI|$)", meta_text
    )
    if high_school_match:
        high_school = high_school_match.group(1).strip()

    rsci_rank = None
    rsci_match = re.search(r"RSCI Top 100:\s*(\d+)", meta_text)
    if rsci_match:
        rsci_rank = int(rsci_match.group(1))

    position = ""
    position_match = re.search(r"Position:\s*([A-Za-z /-]+)", meta_text)
    if position_match:
        position = position_match.group(1).strip()

    return CbbProfile(
        source_url=url,
        cbb_slug=url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".html"),
        display_name=name.get_text(" ", strip=True),
        school=school,
        position=position,
        height_in=_parse_height_in(meta_text),
        weight_lb=_parse_weight_lb(meta_text),
        high_school=high_school,
        hometown="",
        rsci_rank=rsci_rank,
        stats=_parse_stats(soup),
    )


def _candidate_links(index_html: str, source_name: str) -> list[str]:
    soup = BeautifulSoup(index_html, "html.parser")
    normalized_source = normalize_player_name(source_name)
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if not href.startswith("/cbb/players/") or not href.endswith(".html"):
            continue
        if normalize_player_name(anchor.get_text(" ", strip=True)) != normalized_source:
            continue
        links.append(urljoin(CBB_BASE_URL, href))
    return list(dict.fromkeys(links))


def _school_matches(profile: CbbProfile, canonical_affiliation: str) -> bool:
    if not canonical_affiliation:
        return False
    profile_school_key = normalize_player_name(profile.school, ignore_suffix=False)
    canonical_key = normalize_player_name(canonical_affiliation, ignore_suffix=False)
    return bool(
        profile_school_key and canonical_key and profile_school_key == canonical_key
    )


def resolve_cbb_profile(
    source_name: str,
    canonical_affiliation: str,
    *,
    refresh: bool,
    throttle: float,
) -> tuple[CbbProfile | None, str]:
    """Resolve a Top 100 row to one CBB profile."""
    normalized = normalize_player_name(source_name).split()
    if not normalized:
        return None, "empty normalized name"
    last_initial = normalized[-1][0]
    index_url = f"{CBB_BASE_URL}/cbb/players/{last_initial}-index.html"
    index_html = _fetch(index_url, refresh=refresh, throttle=throttle)
    if not index_html:
        return None, f"failed to fetch index {index_url}"

    candidates = _candidate_links(index_html, source_name)
    if not candidates:
        return None, "no CBB index candidate"

    parsed_profiles: list[CbbProfile] = []
    for url in candidates:
        html = _fetch(url, refresh=refresh, throttle=throttle)
        if not html:
            continue
        profile = _parse_profile(url, html)
        if profile:
            parsed_profiles.append(profile)

    if not parsed_profiles:
        return None, "candidate pages did not parse"

    school_matches = [
        profile
        for profile in parsed_profiles
        if _school_matches(profile, canonical_affiliation)
    ]
    if len(school_matches) == 1:
        return school_matches[0], ""
    if len(parsed_profiles) == 1:
        return parsed_profiles[0], "single candidate; school not confirmed"
    return (
        None,
        f"ambiguous CBB candidates: {','.join(p.source_url for p in parsed_profiles)}",
    )


async def _ensure_external_id(conn: Any, player_id: int, profile: CbbProfile) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO player_external_ids (player_id, system, external_id, source_url)
            VALUES (:player_id, :system, :external_id, :source_url)
            ON CONFLICT (system, external_id) DO UPDATE
            SET player_id = EXCLUDED.player_id,
                source_url = EXCLUDED.source_url
            """
        ),
        {
            "player_id": player_id,
            "system": CBB_SOURCE,
            "external_id": profile.cbb_slug,
            "source_url": profile.source_url,
        },
    )


async def _ensure_alias(conn: Any, player_id: int, full_name: str) -> None:
    parsed = parse_player_name(full_name)
    await conn.execute(
        text(
            """
            INSERT INTO player_aliases
                (player_id, full_name, first_name, middle_name, last_name, suffix, context, created_at)
            VALUES
                (:player_id, :full_name, :first_name, :middle_name, :last_name, :suffix, :context, now())
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "player_id": player_id,
            "full_name": full_name,
            "first_name": parsed.first_name or None,
            "middle_name": parsed.middle_name,
            "last_name": parsed.last_name,
            "suffix": parsed.suffix,
            "context": CBB_SOURCE,
        },
    )


async def _update_identity_status(
    conn: Any,
    *,
    player_id: int,
    source_name: str,
    canonical_affiliation: str,
    raw_affiliation: str,
    profile: CbbProfile,
) -> None:
    parsed = parse_player_name(source_name)
    now = datetime.now(UTC).replace(tzinfo=None)
    await conn.execute(
        text(
            """
            UPDATE players_master
            SET first_name = coalesce(first_name, :first_name),
                middle_name = coalesce(middle_name, :middle_name),
                last_name = coalesce(last_name, :last_name),
                suffix = coalesce(suffix, :suffix),
                school = :school,
                school_raw = :school_raw,
                high_school = coalesce(high_school, :high_school),
                rsci_rank = coalesce(rsci_rank, :rsci_rank),
                bio_source = :bio_source,
                is_stub = false,
                enrichment_attempted_at = :now,
                updated_at = :now
            WHERE id = :player_id
            """
        ),
        {
            "player_id": player_id,
            "first_name": parsed.first_name or None,
            "middle_name": parsed.middle_name,
            "last_name": parsed.last_name,
            "suffix": parsed.suffix,
            "school": canonical_affiliation,
            "school_raw": raw_affiliation,
            "high_school": profile.high_school or None,
            "rsci_rank": profile.rsci_rank,
            "bio_source": CBB_SOURCE,
            "now": now,
        },
    )
    await conn.execute(
        text(
            """
            INSERT INTO player_status
                (player_id, raw_position, height_in, weight_lb, source, updated_at)
            VALUES
                (:player_id, :raw_position, :height_in, :weight_lb, :source, :now)
            ON CONFLICT (player_id) DO UPDATE
            SET raw_position = coalesce(EXCLUDED.raw_position, player_status.raw_position),
                height_in = coalesce(EXCLUDED.height_in, player_status.height_in),
                weight_lb = coalesce(EXCLUDED.weight_lb, player_status.weight_lb),
                source = :source,
                updated_at = :now
            """
        ),
        {
            "player_id": player_id,
            "raw_position": profile.position or None,
            "height_in": profile.height_in,
            "weight_lb": profile.weight_lb,
            "source": CBB_SOURCE,
            "now": now,
        },
    )


async def _upsert_stats(conn: Any, player_id: int, stats: CbbStatsRow) -> None:
    values = asdict(stats)
    await conn.execute(
        text(
            """
            INSERT INTO player_college_stats
                (
                    player_id, season, games, games_started, mpg, ppg, rpg, apg,
                    spg, bpg, tov, pf, fg_pct, three_p_pct, three_pa, ft_pct,
                    fta, source, updated_at
                )
            VALUES
                (
                    :player_id, :season, :games, :games_started, :mpg, :ppg, :rpg,
                    :apg, :spg, :bpg, :tov, :pf, :fg_pct, :three_p_pct, :three_pa,
                    :ft_pct, :fta, :source, :now
                )
            ON CONFLICT ON CONSTRAINT uq_college_stats_player_season DO UPDATE
            SET games = EXCLUDED.games,
                games_started = EXCLUDED.games_started,
                mpg = EXCLUDED.mpg,
                ppg = EXCLUDED.ppg,
                rpg = EXCLUDED.rpg,
                apg = EXCLUDED.apg,
                spg = EXCLUDED.spg,
                bpg = EXCLUDED.bpg,
                tov = EXCLUDED.tov,
                pf = EXCLUDED.pf,
                fg_pct = EXCLUDED.fg_pct,
                three_p_pct = EXCLUDED.three_p_pct,
                three_pa = EXCLUDED.three_pa,
                ft_pct = EXCLUDED.ft_pct,
                fta = EXCLUDED.fta,
                source = EXCLUDED.source,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "player_id": player_id,
            **values,
            "source": CBB_SOURCE,
            "now": datetime.now(UTC).replace(tzinfo=None),
        },
    )


async def run_enrichment(
    *,
    database_url: str,
    output_date: date,
    execute: bool,
    limit: int | None,
    refresh: bool,
    throttle: float,
) -> Path:
    """Run Top 100 CBB enrichment and write a review artifact."""
    resolved_rows = await resolve_rows(database_url)
    source_by_rank = {row.source_rank: row for row in TOP100_ROWS}
    mapping = load_school_mapping()
    college_school_names = load_college_school_names()
    review_rows: list[ReviewRow] = []

    cleaned_url, connect_args = _prepare_connection(database_url)
    engine = create_async_engine(cleaned_url, echo=False, connect_args=connect_args)
    processed = 0
    try:
        async with engine.begin() as conn:
            for resolved in resolved_rows:
                source_row = source_by_rank[resolved.source_rank]
                affiliation = resolve_affiliation(
                    source_row.source_affiliation,
                    mapping,
                    college_school_names,
                )
                if limit is not None and processed >= limit:
                    break
                if resolved.player_id is None or resolved.match_status != "matched":
                    review_rows.append(
                        ReviewRow(
                            source_rank=resolved.source_rank,
                            source_name=resolved.source_name,
                            player_id=resolved.player_id,
                            raw_affiliation=resolved.raw_affiliation,
                            canonical_affiliation=resolved.canonical_affiliation,
                            status="skipped",
                            reason=resolved.reason,
                        )
                    )
                    continue
                if affiliation.affiliation_type != "college":
                    review_rows.append(
                        ReviewRow(
                            source_rank=resolved.source_rank,
                            source_name=resolved.source_name,
                            player_id=resolved.player_id,
                            raw_affiliation=resolved.raw_affiliation,
                            canonical_affiliation=resolved.canonical_affiliation,
                            status="skipped",
                            reason="non-college affiliation; requires separate official/pro source",
                        )
                    )
                    continue

                processed += 1
                profile, warning = resolve_cbb_profile(
                    resolved.source_name,
                    resolved.canonical_affiliation,
                    refresh=refresh,
                    throttle=throttle,
                )
                if profile is None:
                    review_rows.append(
                        ReviewRow(
                            source_rank=resolved.source_rank,
                            source_name=resolved.source_name,
                            player_id=resolved.player_id,
                            raw_affiliation=resolved.raw_affiliation,
                            canonical_affiliation=resolved.canonical_affiliation,
                            status="needs_review",
                            reason=warning,
                        )
                    )
                    continue

                db_action = "would_update"
                if execute:
                    await _ensure_external_id(conn, resolved.player_id, profile)
                    await _ensure_alias(conn, resolved.player_id, profile.display_name)
                    await _update_identity_status(
                        conn,
                        player_id=resolved.player_id,
                        source_name=resolved.source_name,
                        canonical_affiliation=resolved.canonical_affiliation,
                        raw_affiliation=resolved.raw_affiliation,
                        profile=profile,
                    )
                    if profile.stats is not None:
                        await _upsert_stats(conn, resolved.player_id, profile.stats)
                    db_action = "updated"

                review_rows.append(
                    ReviewRow(
                        source_rank=resolved.source_rank,
                        source_name=resolved.source_name,
                        player_id=resolved.player_id,
                        raw_affiliation=resolved.raw_affiliation,
                        canonical_affiliation=resolved.canonical_affiliation,
                        status="resolved" if profile.stats else "needs_review",
                        source_url=profile.source_url,
                        cbb_slug=profile.cbb_slug,
                        db_action=db_action,
                        reason=warning
                        or ("" if profile.stats else "stats table missing"),
                        season=profile.stats.season if profile.stats else "",
                        games=profile.stats.games if profile.stats else None,
                        ppg=profile.stats.ppg if profile.stats else None,
                        rpg=profile.stats.rpg if profile.stats else None,
                        apg=profile.stats.apg if profile.stats else None,
                        position=profile.position,
                        height_in=profile.height_in,
                        weight_lb=profile.weight_lb,
                        high_school=profile.high_school,
                        rsci_rank=profile.rsci_rank,
                    )
                )

            if not execute:
                await conn.rollback()
    finally:
        await engine.dispose()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    mode = "execute" if execute else "dry_run"
    path = OUTPUT_DIR / f"top100_cbb_enrichment_{mode}_{output_date.isoformat()}.csv"
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(asdict(review_rows[0])))
        writer.writeheader()
        for row in review_rows:
            writer.writerow(asdict(row))
    return path


def parse_args() -> argparse.Namespace:
    """Parse CLI args."""
    parser = argparse.ArgumentParser(
        description="Enrich Top 100 from CBB Sports-Reference"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Apply database updates")
    mode.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--throttle", type=float, default=1.0)
    return parser.parse_args()


def main() -> None:
    """CLI entrypoint."""
    load_dotenv()
    args = parse_args()
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    try:
        path = asyncio.run(
            run_enrichment(
                database_url=database_url,
                output_date=args.date,
                execute=args.execute,
                limit=args.limit,
                refresh=args.refresh,
                throttle=args.throttle,
            )
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(path)


if __name__ == "__main__":
    main()
