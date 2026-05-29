"""Ingest Draft Combine CSVs (anthro, agility, shooting) into the database.

Usage:
  python scripts/ingest_combine.py --out-dir scraper/output [--season 2024-25] [--source anthro|agility|shooting|all]

Notes:
  - Uses app.utils.db_async for async engine/session.
  - Idempotent upserts on (player_id, season_id[, drill]).
  - Creates seasons and player identities as needed.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.db_async import get_session
from app.schemas.seasons import Season
from app.schemas.players_master import PlayerMaster
from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.combine_anthro import CombineAnthro
from app.schemas.combine_agility import CombineAgility
from app.schemas.combine_shooting import (
    CombineShooting,
    SHOOTING_DRILL_COLUMNS,
    SHOOTING_PCT_COLUMNS,
    compute_shooting_pct,
)
from app.schemas.positions import Position
from app.models.position_taxonomy import derive_position_tags, get_parents_for_fine
from app.services.player_mention_service import (
    build_player_name_lookup,
    find_existing_player,
    parse_player_name,
    register_player_in_lookup,
)

logger = logging.getLogger(__name__)

# Suffix tokens (normalized, sans punctuation) used to detect a suffix that
# leaked into the prefix/first source column.
_SUFFIX_TOKENS = {"jr", "sr", "ii", "iii", "iv", "v"}


# ------------------------------
# Helpers
# ------------------------------


def _display_name(
    prefix: Optional[str],
    first: Optional[str],
    middle: Optional[str],
    last: Optional[str],
    suffix: Optional[str],
) -> str:
    parts = [p for p in [prefix, first, middle, last, suffix] if p]
    return " ".join(parts)


def _normalize_leading_suffix(name: str) -> str:
    """Move a leading suffix token to the end of a full-name string.

    A mis-mapped source produced suffix-first names like "Jr Morez Johnson"
    (which then drove the mangled slug ``jr-morez-johnson`` and missed the
    canonical "Morez Johnson Jr."). Normalizing the *name string* — rather than
    the structured fields — fixes it regardless of whether the mangled value
    came from ``player_name`` or the split columns, since this single value
    drives both matching and the created record. Only fires for 3+ tokens so a
    two-word name is never reordered. Returns ``name`` unchanged otherwise.
    """
    tokens = name.split()
    if len(tokens) >= 3 and tokens[0].strip(".").lower() in _SUFFIX_TOKENS:
        return " ".join(tokens[1:] + [tokens[0]])
    return name


async def get_or_create_season(session: AsyncSession, code: str) -> Season:
    # Expect code format 'YYYY-YY'
    stmt = select(Season).where(Season.code == code)
    res = await session.execute(stmt)
    season = res.scalar_one_or_none()
    if season:
        return season
    try:
        start = int(code.split("-")[0])
    except Exception:
        # fallback: duplicate end year
        start = int(code[:4]) if code and code[:4].isdigit() else 0
    end = start + 1 if start else start
    season = Season(code=code, start_year=start, end_year=end)
    session.add(season)
    await session.flush()
    return season


async def find_player_by_external(
    session: AsyncSession, system: str, external_id: str
) -> Optional[PlayerMaster]:
    stmt = (
        select(PlayerMaster)
        .join(PlayerExternalId, PlayerExternalId.player_id == PlayerMaster.id)
        .where(
            and_(
                PlayerExternalId.system == system,
                PlayerExternalId.external_id == external_id,
            )
        )
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


def _external_id_conflict(existing_ids: List[str], external_id: Optional[str]) -> bool:
    """Return whether ``external_id`` conflicts with a player's existing ids.

    A conflict means the player already carries an id of the same system that
    differs from ``external_id`` — a signal the row is a *distinct* identity, so
    a relaxed name match should not merge them. Pure for easy testing.
    """
    if external_id is None:
        return False
    return any(eid != external_id for eid in existing_ids)


async def _player_external_id_conflicts(
    session: AsyncSession,
    *,
    player_id: int,
    system: str,
    external_id: Optional[str],
) -> bool:
    """Whether the matched player already has a *different* id of ``system``."""
    if external_id is None:
        return False
    rows = (
        (
            await session.execute(
                select(PlayerExternalId.external_id).where(
                    and_(
                        PlayerExternalId.player_id == player_id,
                        PlayerExternalId.system == system,
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    return _external_id_conflict(list(rows), external_id)


async def _ensure_external_id(
    session: AsyncSession,
    *,
    player_id: int,
    system: str,
    external_id: str,
) -> None:
    """Attach ``external_id`` to a player if it isn't already recorded.

    Called when a combine row links to a player by name rather than by id, so
    the canonical record gains the source id and later ``find_player_by_external``
    lookups resolve directly instead of falling back to name matching.
    """
    already = (
        await session.execute(
            select(PlayerExternalId.id).where(
                and_(
                    PlayerExternalId.player_id == player_id,
                    PlayerExternalId.system == system,
                    PlayerExternalId.external_id == external_id,
                )
            )
        )
    ).first()
    if already is None:
        session.add(
            PlayerExternalId(
                player_id=player_id, system=system, external_id=external_id
            )
        )


async def get_or_create_player(
    session: AsyncSession,
    prefix: Optional[str],
    first: Optional[str],
    middle: Optional[str],
    last: Optional[str],
    suffix: Optional[str],
    *,
    nba_stats_player_id: Optional[str] = None,
    raw_player_name: Optional[str] = None,
    name_lookup: Optional[object] = None,
) -> Optional[PlayerMaster]:
    """Resolve a combine row to an existing player, or create one if genuinely new.

    Matching order:
      1. ``nba_stats`` external id (most reliable).
      2. Normalized name match via the shared, suffix- and punctuation-aware
         matcher (:func:`find_existing_player`). This is what prevents
         re-creating a duplicate of a player that already exists under a
         suffix/punctuation variant (e.g. "Darius Acuff Jr." vs "Darius Acuff").
      3. Create a new ``PlayerMaster`` only when no existing player matched.

    Returns ``None`` (and logs) when the name is **ambiguous** (matches more
    than one player): minting would add a duplicate, so the row is skipped for
    manual review instead — per the project's "ambiguous → never guess" rule.

    Args:
        session: Async database session; the caller owns the transaction.
        prefix: Name prefix from the source row (e.g. a title), if any.
        first: First-name field from the source row.
        middle: Middle-name field from the source row.
        last: Last-name field from the source row.
        suffix: Suffix field from the source row (e.g. "Jr.").
        nba_stats_player_id: NBA Stats external id, used for the most reliable
            match when present.
        raw_player_name: The source's full name string; preferred over the
            individual name fields for matching and display.
        name_lookup: Optional prebuilt lookup (from
            :func:`build_player_name_lookup`) for batch imports; newly created
            players are registered back into it so later rows in the same batch
            match them.

    Returns:
        The matched or newly created ``PlayerMaster``, or ``None`` when the row
        is skipped (ambiguous name or no usable name).
    """
    # 1) Try external id linkage.
    if nba_stats_player_id:
        pm = await find_player_by_external(
            session, system="nba_stats", external_id=str(nba_stats_player_id)
        )
        if pm:
            return pm

    # 2) Normalized name match (suffix/punctuation-insensitive, ambiguity-aware).
    #    Normalize a leaked leading suffix on the name string itself so a mangled
    #    "Jr Morez Johnson" (from either player_name or the split columns) becomes
    #    "Morez Johnson Jr" for both matching and the created record.
    full_name = _normalize_leading_suffix(
        (
            raw_player_name or _display_name(prefix, first, middle, last, suffix) or ""
        ).strip()
    )
    if full_name:
        match, ambiguous = await find_existing_player(
            session,
            full_name,
            lookup=name_lookup,  # type: ignore[arg-type]
        )
        if match is not None:
            existing = await session.get(PlayerMaster, match.player_id)
            # Guard against merging distinct identities: if this row carries an
            # external id and the matched player already has a *different* id of
            # the same system, they are not the same person (e.g. a new
            # "Gary Payton II" vs an existing "Gary Payton"). Fall through to
            # create a new record. When the matched player has no conflicting id
            # (the common case — a prospect not yet linked), link as usual so a
            # suffix variant like "Darius Acuff" still dedups to "Darius Acuff Jr.".
            if existing is not None and not await _player_external_id_conflicts(
                session,
                player_id=match.player_id,
                system="nba_stats",
                external_id=str(nba_stats_player_id) if nba_stats_player_id else None,
            ):
                # Persist the row's external id onto the name-matched player so
                # future external-id lookups resolve directly (and don't risk a
                # duplicate if the source name later changes).
                if nba_stats_player_id:
                    await _ensure_external_id(
                        session,
                        player_id=match.player_id,
                        system="nba_stats",
                        external_id=str(nba_stats_player_id),
                    )
                return existing
        elif ambiguous:
            logger.warning(
                "combine.ingest.ambiguous_name name=%r — skipping row to avoid "
                "creating a duplicate; resolve manually",
                full_name,
            )
            return None

    # 3) Create a new player. ``full_name`` was built from the already-corrected
    #    fields above, so the display name, alias, and slug are well-formed.
    display_name = full_name or _display_name(prefix, first, middle, last, suffix)
    if not display_name:
        logger.warning("combine.ingest.no_name — skipping row with no usable name")
        return None
    parsed = parse_player_name(display_name)
    # The name string was normalized above, but a suffix token can still sit in
    # the structured prefix column. Drop it so we don't persist prefix='Jr'
    # alongside the parsed suffix='Jr.'.
    if prefix and prefix.strip(".").lower() in _SUFFIX_TOKENS:
        prefix = None
    pm3 = PlayerMaster(
        prefix=prefix,
        first_name=parsed.first_name or first,
        middle_name=parsed.middle_name or middle,
        last_name=parsed.last_name or last,
        suffix=parsed.suffix or suffix,
        display_name=display_name,
    )
    session.add(pm3)
    await session.flush()
    # seed alias
    if display_name:
        session.add(
            PlayerAlias(
                player_id=pm3.id,
                full_name=display_name,
                prefix=prefix,
                first_name=parsed.first_name or first,
                middle_name=parsed.middle_name or middle,
                last_name=parsed.last_name or last,
                suffix=parsed.suffix or suffix,
                context="scraper",
            )
        )
    # seed external id
    if nba_stats_player_id:
        session.add(
            PlayerExternalId(
                player_id=pm3.id,
                system="nba_stats",
                external_id=str(nba_stats_player_id),
            )
        )
    # Keep a batch lookup consistent so later rows match this new player.
    if name_lookup is not None and pm3.id is not None:
        register_player_in_lookup(name_lookup, pm3.id, display_name)  # type: ignore[arg-type]
    return pm3


async def get_or_create_position_id(
    session: AsyncSession, fine_code: Optional[str]
) -> Optional[int]:
    if not fine_code:
        return None
    stmt = select(Position).where(Position.code == fine_code)
    res = await session.execute(stmt)
    pos = res.scalar_one_or_none()
    if pos:
        return pos.id
    # Create
    parents = get_parents_for_fine(fine_code)
    pos = Position(code=fine_code, parents=parents)
    session.add(pos)
    await session.flush()
    return pos.id


# ------------------------------
# CSV Readers
# ------------------------------


def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        return [dict(row) for row in rdr]


def _to_opt_float(v: Optional[str]) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def _to_opt_int(v: Optional[str]) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(v))  # tolerate '10.0'
    except Exception:
        return None


# ------------------------------
# Ingestors
# ------------------------------


async def ingest_anthro(session: AsyncSession, rows: List[Dict[str, str]]) -> int:
    count = 0
    name_lookup = await build_player_name_lookup(session)
    for row in rows:
        season_code = row.get("season") or ""
        season = await get_or_create_season(session, season_code)

        pm = await get_or_create_player(
            session,
            prefix=row.get("prefix"),
            first=row.get("first_name"),
            middle=row.get("middle_name"),
            last=row.get("last_name"),
            suffix=row.get("suffix"),
            nba_stats_player_id=row.get("player_id") or row.get("person_id"),
            raw_player_name=row.get("player_name"),
            name_lookup=name_lookup,
        )
        if pm is None:
            continue

        stmt = select(CombineAnthro).where(
            and_(CombineAnthro.player_id == pm.id, CombineAnthro.season_id == season.id)
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        raw_position, position_fine, position_parents = _position_triplet(
            row.get("pos")
        )
        position_id = await get_or_create_position_id(session, position_fine)
        payload = {
            "player_id": pm.id,
            "season_id": season.id,
            "position_id": position_id,
            "raw_position": raw_position,
            "body_fat_pct": _to_opt_float(row.get("body_fat_pct")),
            "hand_length_in": _to_opt_float(row.get("hand_length")),
            "hand_width_in": _to_opt_float(row.get("hand_width")),
            "height_wo_shoes_in": _to_opt_float(row.get("height_wo_shoes")),
            "height_w_shoes_in": _to_opt_float(row.get("height_w_shoes")),
            "standing_reach_in": _to_opt_float(row.get("standing_reach")),
            "wingspan_in": _to_opt_float(row.get("wingspan")),
            "weight_lb": _to_opt_float(row.get("weight")),
            "nba_stats_player_id": _to_opt_int(row.get("player_id")),
            "raw_player_name": row.get("player_name"),
        }
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
        else:
            session.add(CombineAnthro(**payload))
        count += 1
    return count


async def ingest_agility(session: AsyncSession, rows: List[Dict[str, str]]) -> int:
    count = 0
    name_lookup = await build_player_name_lookup(session)
    for row in rows:
        season_code = row.get("season") or ""
        season = await get_or_create_season(session, season_code)

        pm = await get_or_create_player(
            session,
            prefix=row.get("prefix"),
            first=row.get("first_name"),
            middle=row.get("middle_name"),
            last=row.get("last_name"),
            suffix=row.get("suffix"),
            nba_stats_player_id=row.get("player_id") or row.get("person_id"),
            raw_player_name=row.get("player_name"),
            name_lookup=name_lookup,
        )
        if pm is None:
            continue

        stmt = select(CombineAgility).where(
            and_(
                CombineAgility.player_id == pm.id, CombineAgility.season_id == season.id
            )
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        raw_position, position_fine, position_parents = _position_triplet(
            row.get("pos")
        )
        position_id = await get_or_create_position_id(session, position_fine)
        payload = {
            "player_id": pm.id,
            "season_id": season.id,
            "position_id": position_id,
            "raw_position": raw_position,
            "lane_agility_time_s": _to_opt_float(row.get("lane_agility_time")),
            "shuttle_run_s": _to_opt_float(row.get("modified_lane_agility_time")),
            "three_quarter_sprint_s": _to_opt_float(row.get("three_quarter_sprint")),
            "standing_vertical_in": _to_opt_float(row.get("standing_vertical_leap")),
            "max_vertical_in": _to_opt_float(row.get("max_vertical_leap")),
            "bench_press_reps": _to_opt_int(row.get("bench_press")),
            "nba_stats_player_id": _to_opt_int(row.get("player_id")),
            "raw_player_name": row.get("player_name"),
        }
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
        else:
            session.add(CombineAgility(**payload))
        count += 1
    return count


SHOOTING_MAP: List[Tuple[str, str]] = [
    ("off_dribble", "off_the_dribble_shooting"),
    ("spot_up", "spot_up_shooting"),
    ("three_point_star", "three_point_star_drill"),
    ("midrange_star", "mid_range_star_drill"),
    ("three_point_side", "side_mid_side_drill"),
    ("midrange_side", "mid_side_side_drill"),
    ("free_throw", "free_throws"),
]


async def ingest_shooting(session: AsyncSession, rows: List[Dict[str, str]]) -> int:
    count = 0
    name_lookup = await build_player_name_lookup(session)
    for row in rows:
        season_code = row.get("season") or ""
        if not season_code:
            continue
        season = await get_or_create_season(session, season_code)
        pm = await get_or_create_player(
            session,
            prefix=row.get("prefix"),
            first=row.get("first_name"),
            middle=row.get("middle_name"),
            last=row.get("last_name"),
            suffix=row.get("suffix"),
            nba_stats_player_id=row.get("player_id") or row.get("person_id"),
            raw_player_name=row.get("player_name"),
            name_lookup=name_lookup,
        )
        if pm is None:
            continue
        raw_position, position_fine, position_parents = _position_triplet(
            row.get("pos")
        )
        position_id = await get_or_create_position_id(session, position_fine)
        nba_pid = _to_opt_int(row.get("player_id"))
        raw_name = row.get("player_name")

        payload: Dict[str, Optional[int] | Optional[str] | Optional[float]] = {
            "player_id": pm.id,
            "season_id": season.id,
            "position_id": position_id,
            "raw_position": raw_position,
            "nba_stats_player_id": nba_pid,
            "raw_player_name": raw_name,
        }
        for drill_key, base in SHOOTING_MAP:
            fgm = _to_opt_int(row.get(f"{base}_made"))
            fga = _to_opt_int(row.get(f"{base}_attempt"))
            col_pair = SHOOTING_DRILL_COLUMNS[drill_key]
            payload[col_pair[0]] = fgm
            payload[col_pair[1]] = fga
            payload[SHOOTING_PCT_COLUMNS[drill_key]] = compute_shooting_pct(fgm, fga)

        has_data = any(
            payload[col] is not None
            for columns in SHOOTING_DRILL_COLUMNS.values()
            for col in columns
        )
        if not has_data:
            continue

        stmt = select(CombineShooting).where(
            and_(
                CombineShooting.player_id == pm.id,
                CombineShooting.season_id == season.id,
            )
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
        else:
            session.add(CombineShooting(**payload))  # type: ignore[arg-type]
        count += 1
    return count


# ------------------------------
# CLI
# ------------------------------


async def run(out_dir: Path, season: Optional[str], source: str) -> None:
    async for session in get_session():  # type: ignore
        async with session.begin():
            # season filter helper
            def pick(files: List[Path]) -> List[Path]:
                if season:
                    return [p for p in files if p.name.startswith(season)]
                return files

            anthro_files = pick(sorted(out_dir.glob("*_anthro.csv")))
            agility_files = pick(sorted(out_dir.glob("*_agility.csv")))
            shooting_files = pick(sorted(out_dir.glob("*_shooting.csv")))

            total = 0
            if source in {"all", "anthro"}:
                for fp in anthro_files:
                    rows = _read_csv(fp)
                    total += await ingest_anthro(session, rows)
            if source in {"all", "agility"}:
                for fp in agility_files:
                    rows = _read_csv(fp)
                    total += await ingest_agility(session, rows)
            if source in {"all", "shooting"}:
                for fp in shooting_files:
                    rows = _read_csv(fp)
                    total += await ingest_shooting(session, rows)
        # end transaction
        print(f"[ingest] completed with {total} upserts")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest Draft Combine CSVs into database"
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default="scraper/output",
        help="Directory containing CSVs",
    )
    parser.add_argument(
        "--season", type=str, default=None, help="Optional season filter 'YYYY-YY'"
    )
    parser.add_argument(
        "--source",
        type=str,
        choices=["all", "anthro", "agility", "shooting"],
        default="all",
    )
    args = parser.parse_args()
    asyncio.run(run(Path(args.out_dir), args.season, args.source))


def _position_triplet(
    raw_pos: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[List[str]]]:
    fine, parents = derive_position_tags(raw_pos)
    return raw_pos or None, fine, parents or None


if __name__ == "__main__":
    main()
