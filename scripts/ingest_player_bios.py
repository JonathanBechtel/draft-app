import argparse
import asyncio
import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, cast

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.player_bio_snapshots import PlayerBioSnapshot
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.positions import Position
from app.models.position_taxonomy import derive_position_tags
from app.utils.db_async import SessionLocal


SYSTEM_BBR = "bbr"
SYSTEM_X = "x"
SYSTEM_INSTAGRAM = "instagram"


def _norm(s: Optional[str]) -> str:
    if not s:
        return ""
    s2 = s.strip().lower()
    s2 = re.sub(r"[^a-z0-9\s]", "", s2)
    s2 = re.sub(r"\s+", " ", s2)
    return s2


def _name_parts(full_name: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    # Heuristic split: first, middle(s), last
    if not full_name:
        return None, None, None
    tokens = [t for t in re.split(r"\s+", full_name.strip()) if t]
    if not tokens:
        return None, None, None
    if len(tokens) == 1:
        return tokens[0], None, None
    first = tokens[0]
    last = tokens[-1]
    middle = " ".join(tokens[1:-1]) if len(tokens) > 2 else None
    return first, middle, last


def _coerce_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    return value.strip().lower() == "true"


@dataclass
class BioRow:
    slug: str
    url: str
    full_name: str
    birth_date: Optional[str]
    birth_city: Optional[str]
    birth_state_province: Optional[str]
    birth_country: Optional[str]
    shoots: Optional[str]
    school: Optional[str]
    high_school: Optional[str]
    draft_year: Optional[int]
    draft_round: Optional[int]
    draft_pick: Optional[int]
    draft_team: Optional[str]
    nba_debut_date: Optional[str]
    nba_debut_season: Optional[str]
    is_active_nba: Optional[bool]
    current_team: Optional[str]
    nba_last_season: Optional[str]
    position: Optional[str]
    height_in: Optional[int]
    weight_lb: Optional[int]
    social_x_handle: Optional[str]
    social_x_url: Optional[str]
    social_instagram_handle: Optional[str]
    social_instagram_url: Optional[str]
    source_url: str
    scraped_at: str


async def _load_lookup(
    db: AsyncSession,
) -> Tuple[
    Dict[str, int], Dict[str, List[int]], Dict[str, List[int]], Dict[int, PlayerMaster]
]:
    # ext_map: slug -> player_id
    ext_res = await db.execute(
        select(PlayerExternalId).where(
            cast(ColumnElement[bool], PlayerExternalId.system == SYSTEM_BBR)
        )
    )
    ext_map: Dict[str, int] = {}
    for r in ext_res.scalars().all():
        ext_map[r.external_id] = r.player_id

    # alias_map: normalized fullname -> [player_id]
    alias_res = await db.execute(select(PlayerAlias))
    alias_map: Dict[str, List[int]] = defaultdict(list)
    for a in alias_res.scalars().all():
        alias_map[_norm(a.full_name)].append(a.player_id)

    # last_name index from players_master
    master_res = await db.execute(select(PlayerMaster))
    last_name_idx: Dict[str, List[int]] = defaultdict(list)
    pm_by_id: Dict[int, PlayerMaster] = {}
    for p in master_res.scalars().all():
        pid = p.id
        if pid is None:
            continue
        pm_by_id[pid] = p
        # include display_name as alias too
        if p.display_name:
            alias_map[_norm(p.display_name)].append(pid)
        # basic last name index
        if p.last_name:
            last_name_idx[_norm(p.last_name)].append(pid)
        # and 'first last'
        fl = " ".join([t for t in [p.first_name or None, p.last_name or None] if t])
        if fl:
            alias_map[_norm(fl)].append(pid)
    return ext_map, alias_map, last_name_idx, pm_by_id


def _deterministic_match(
    full_name: str,
    last_name_idx: Dict[str, List[int]],
    pm_by_id: Dict[int, PlayerMaster],
) -> Optional[int]:
    first_name, _, last_name = _name_parts(full_name)
    if not last_name:
        return None
    candidates = last_name_idx.get(_norm(last_name), [])
    if not candidates:
        return None
    # Tier 2: last exact + first exact
    for pid in candidates:
        p = pm_by_id.get(pid)
        if p and p.first_name and _norm(p.first_name) == _norm(first_name or ""):
            return pid
    # Tier 3: last exact + first initial
    finitial = (first_name or "").strip().lower()[:1]
    hits = []
    for pid in candidates:
        p = pm_by_id.get(pid)
        if p and p.first_name and p.first_name.strip().lower().startswith(finitial):
            hits.append(pid)
    if len(hits) == 1:
        return hits[0]
    return None


async def _upsert_external(
    db: AsyncSession,
    player_id: int,
    system: str,
    external_id: str,
    source_url: Optional[str],
) -> None:
    res = await db.execute(
        select(PlayerExternalId).where(
            cast(ColumnElement[bool], PlayerExternalId.system == system),
            cast(ColumnElement[bool], PlayerExternalId.external_id == external_id),
        )
    )
    row = res.scalars().first()
    if row:
        if row.player_id != player_id:
            # keep original; do not reassign automatically
            return
        # update source_url if missing
        if source_url and row.source_url != source_url:
            row.source_url = source_url
        return
    db.add(
        PlayerExternalId(
            player_id=player_id,
            system=system,
            external_id=external_id,
            source_url=source_url,
        )
    )


async def _ensure_alias(db: AsyncSession, player_id: int, full_name: str) -> None:
    res = await db.execute(
        select(PlayerAlias).where(
            cast(ColumnElement[bool], PlayerAlias.player_id == player_id),
            cast(ColumnElement[bool], PlayerAlias.full_name == full_name),
        )
    )
    if res.scalars().first() is None:
        db.add(
            PlayerAlias(
                player_id=player_id,
                full_name=full_name,
                first_name=_name_parts(full_name)[0],
                last_name=_name_parts(full_name)[2],
                context="bbr",
            )
        )


async def _update_master(
    db: AsyncSession, player: PlayerMaster, row: BioRow, overwrite: bool
) -> None:
    # Normalize location artifacts that may be present in CSVs generated before parser fixes
    def _clean_loc(
        city: Optional[str], state: Optional[str], country: Optional[str]
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        st = state or None
        co = country or None
        if st:
            m = re.search(r"\b(us|usa|canada|mexico)\b\s*$", st, flags=re.IGNORECASE)
            if m:
                token = m.group(1).upper()
                st = re.sub(
                    r"\b(us|usa|canada|mexico)\b\s*$", "", st, flags=re.IGNORECASE
                ).rstrip()
                if not co:
                    co = "US" if token in {"US", "USA"} else token.title()
        return city or None, st, co

    row.birth_city, row.birth_state_province, row.birth_country = _clean_loc(
        row.birth_city, row.birth_state_province, row.birth_country
    )
    # birthdate
    if row.birth_date:
        if player.birthdate is None or overwrite:
            # parse ISO date
            try:
                y, m, d = [int(x) for x in row.birth_date.split("-")]
                from datetime import date as _date

                player.birthdate = _date(y, m, d)
            except Exception:
                pass
    # nba debut date
    if row.nba_debut_date:
        if player.nba_debut_date is None or overwrite:
            try:
                y, m, d = [int(x) for x in row.nba_debut_date.split("-")]
                from datetime import date as _date

                player.nba_debut_date = _date(y, m, d)
            except Exception:
                pass

    # immutable fields: only set if null unless overwrite
    def set_if_null(attr: str, value):
        if value is None:
            return
        cur = getattr(player, attr)
        if cur is None or overwrite:
            setattr(player, attr, value)

    set_if_null("birth_city", row.birth_city)
    set_if_null("birth_state_province", row.birth_state_province)
    set_if_null("birth_country", row.birth_country)
    set_if_null("shoots", row.shoots)
    set_if_null("school", row.school)
    set_if_null("high_school", row.high_school)
    set_if_null("draft_year", row.draft_year)
    set_if_null("draft_round", row.draft_round)
    set_if_null("draft_pick", row.draft_pick)
    set_if_null("draft_team", row.draft_team)
    set_if_null("nba_debut_season", row.nba_debut_season)


async def _upsert_status(db: AsyncSession, player_id: int, row: BioRow) -> None:
    res = await db.execute(
        select(PlayerStatus).where(
            cast(ColumnElement[bool], PlayerStatus.player_id == player_id)
        )
    )
    status = res.scalars().first()
    if not status:
        status = PlayerStatus(player_id=player_id)
        db.add(status)
    status.is_active_nba = row.is_active_nba
    status.current_team = row.current_team
    status.nba_last_season = row.nba_last_season
    status.position = row.position

    # Resolve position_id
    if row.position:
        fine, _ = derive_position_tags(row.position)
        if fine:
            # Find or create position
            pos_res = await db.execute(select(Position).where(Position.code == fine))
            pos = pos_res.scalar_one_or_none()
            if not pos:
                pos = Position(code=fine)
                db.add(pos)
                await db.flush()
            status.position_id = pos.id

    status.height_in = int(row.height_in) if row.height_in is not None else None
    status.weight_lb = int(row.weight_lb) if row.weight_lb is not None else None
    status.source = "bbr"


def _load_raw_meta_html(cache_dir: Path, slug: str) -> Optional[str]:
    path = cache_dir / f"{slug}.html"
    if not path.exists():
        return None
    try:
        html = path.read_text(encoding="utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("div", id="meta")
        return str(meta) if meta else None
    except Exception:
        return None


async def ingest(
    csv_path: Path,
    cache_dir: Path,
    dry_run: bool,
    verbose: bool,
    overwrite_master: bool,
    fix_ambiguities_path: Optional[Path],
    create_missing: bool,
) -> None:
    # Read CSV
    rows: List[BioRow] = []
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for d in reader:
            rows.append(
                BioRow(
                    slug=d.get("slug") or "",
                    url=d.get("url") or d.get("source_url") or "",
                    full_name=d.get("full_name") or "",
                    birth_date=d.get("birth_date") or None,
                    birth_city=d.get("birth_city") or None,
                    birth_state_province=d.get("birth_state_province") or None,
                    birth_country=d.get("birth_country") or None,
                    shoots=d.get("shoots") or None,
                    school=d.get("school") or None,
                    high_school=d.get("high_school") or None,
                    draft_year=int(d["draft_year"]) if d.get("draft_year") else None,
                    draft_round=int(d["draft_round"]) if d.get("draft_round") else None,
                    draft_pick=int(d["draft_pick"]) if d.get("draft_pick") else None,
                    draft_team=d.get("draft_team") or None,
                    nba_debut_date=d.get("nba_debut_date") or None,
                    nba_debut_season=d.get("nba_debut_season") or None,
                    is_active_nba=_coerce_bool(d.get("is_active_nba")),
                    current_team=d.get("current_team") or None,
                    nba_last_season=d.get("nba_last_season") or None,
                    position=d.get("position") or None,
                    height_in=int(float(d["height_in"]))
                    if d.get("height_in")
                    else None,
                    weight_lb=int(float(d["weight_lb"]))
                    if d.get("weight_lb")
                    else None,
                    social_x_handle=d.get("social_x_handle") or None,
                    social_x_url=d.get("social_x_url") or None,
                    social_instagram_handle=d.get("social_instagram_handle") or None,
                    social_instagram_url=d.get("social_instagram_url") or None,
                    source_url=d.get("source_url") or d.get("url") or "",
                    scraped_at=d.get("scraped_at") or "",
                )
            )

    # Ambiguities fixes: mapping slug -> player_id
    fixed_map: Dict[str, int] = {}
    if fix_ambiguities_path and fix_ambiguities_path.exists():
        fixed_map = json.loads(fix_ambiguities_path.read_text(encoding="utf-8"))

    async with SessionLocal() as db:
        ext_map, alias_map, last_idx, pm_by_id = await _load_lookup(db)
        unmatched: List[str] = []
        ambiguous: List[str] = []

        for r in rows:
            player_id: Optional[int] = None
            # Fixed mapping overrides
            if r.slug in fixed_map:
                player_id = fixed_map[r.slug]
            # External ID (bbr)
            if player_id is None:
                player_id = ext_map.get(r.slug)
            # Exact alias
            if player_id is None:
                pids = alias_map.get(_norm(r.full_name), [])
                if len(pids) == 1:
                    player_id = pids[0]
                elif len(pids) > 1:
                    ambiguous.append(r.slug)
            # Deterministic match by last/first
            if player_id is None:
                pid = _deterministic_match(r.full_name, last_idx, pm_by_id)
                if pid is not None:
                    player_id = pid

            if player_id is None:
                if create_missing:
                    # Create a new PlayerMaster for this record (canonical row)
                    first, middle, last = _name_parts(r.full_name)
                    pm = PlayerMaster(
                        prefix=None,
                        first_name=first,
                        middle_name=middle,
                        last_name=last,
                        suffix=None,
                        display_name=r.full_name,
                    )
                    db.add(pm)
                    await db.flush()  # get pm.id
                    player_pk = pm.id
                    if player_pk is None:
                        raise RuntimeError("PlayerMaster missing id after flush")
                    player_id = player_pk
                    pm_by_id[player_pk] = pm
                    # Update lookups for subsequent rows
                    alias_map.setdefault(_norm(r.full_name), []).append(player_pk)
                    if last:
                        last_idx.setdefault(_norm(last), []).append(player_pk)
                else:
                    unmatched.append(r.slug)
                    if verbose:
                        print(f"[warn] unmatched slug={r.slug} name={r.full_name}")
                    continue

            assert player_id is not None
            # Load player
            player = pm_by_id.get(player_id)
            if not player:
                # Should not happen
                unmatched.append(r.slug)
                continue

            # External IDs: bbr + socials
            await _upsert_external(db, player_id, SYSTEM_BBR, r.slug, r.source_url)
            if r.social_x_handle:
                await _upsert_external(
                    db, player_id, SYSTEM_X, r.social_x_handle, r.social_x_url
                )
            if r.social_instagram_handle:
                await _upsert_external(
                    db,
                    player_id,
                    SYSTEM_INSTAGRAM,
                    r.social_instagram_handle,
                    r.social_instagram_url,
                )

            await _ensure_alias(db, player_id, r.full_name)

            # Update master (immutable fields)
            await _update_master(db, player, r, overwrite_master)

            # Upsert status (ephemeral)
            await _upsert_status(db, player_id, r)

            # Snapshot raw meta HTML if present in cache
            raw_meta = _load_raw_meta_html(cache_dir, r.slug)
            if raw_meta:
                db.add(
                    PlayerBioSnapshot(
                        player_id=player_id,
                        source="bbr",
                        source_url=r.source_url,
                        raw_meta_html=raw_meta,
                    )
                )

        if dry_run:
            await db.rollback()
        else:
            try:
                await db.commit()
            except IntegrityError as e:
                await db.rollback()
                raise e

        # Emit reports
        if unmatched:
            (csv_path.parent / "bbio_unmatched.json").write_text(
                json.dumps(unmatched, indent=2), encoding="utf-8"
            )
        if ambiguous:
            (csv_path.parent / "bbio_ambiguous.json").write_text(
                json.dumps(ambiguous, indent=2), encoding="utf-8"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest BBR bio CSV into database")
    parser.add_argument("--file", required=True, type=str, help="Path to bbio CSV file")
    parser.add_argument(
        "--cache-dir",
        type=str,
        default="scraper/cache/players",
        help="Directory containing cached player HTML for snapshots",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Do not commit DB changes"
    )
    parser.add_argument(
        "--overwrite-master",
        action="store_true",
        help="Allow overwriting master fields",
    )
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help="Create players_master rows for unmatched records",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--fix-ambiguities",
        type=str,
        default=None,
        help="Path to JSON mapping of slug -> player_id for manual resolutions",
    )
    args = parser.parse_args()

    asyncio.run(
        ingest(
            csv_path=Path(args.file),
            cache_dir=Path(args.cache_dir),
            dry_run=args.dry_run,
            verbose=args.verbose,
            overwrite_master=args.overwrite_master,
            fix_ambiguities_path=Path(args.fix_ambiguities)
            if args.fix_ambiguities
            else None,
            create_missing=args.create_missing,
        )
    )


if __name__ == "__main__":
    main()
