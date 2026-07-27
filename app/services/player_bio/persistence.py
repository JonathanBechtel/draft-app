"""The database writes one resolved bio row performs.

Every function here takes an ``AsyncSession`` and stages changes on it; the
caller (``ingest``) owns the commit, so a whole CSV lands or none of it does.
``players_master`` fields are treated as immutable by default -- only filled in
when null -- while ``player_status`` is ephemeral and always overwritten.
"""

import re
from pathlib import Path
from typing import Optional, Tuple, cast

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.position_taxonomy import derive_position_tags, get_parents_for_fine
from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.player_status import PlayerStatus
from app.schemas.players_master import PlayerMaster
from app.schemas.positions import Position
from app.services.canonical_resolution_service import (
    load_college_school_names,
    load_school_mapping,
    resolve_affiliation,
)
from app.services.player_bio.matching import _name_parts
from app.services.player_bio.rows import BioRow
from app.utils.country import canonical_country


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
    # Normalize to a canonical country name (ISO codes/aliases → full name) so
    # facets and filters stay consistent regardless of source encoding.
    row.birth_country = canonical_country(row.birth_country)
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
    if row.school:
        school_resolution = resolve_affiliation(
            row.school,
            load_school_mapping(),
            load_college_school_names(),
        )
        if school_resolution.affiliation_type == "college":
            set_if_null("school", school_resolution.canonical_affiliation)
            set_if_null("school_raw", row.school)
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
    status.raw_position = row.position

    # Resolve position_id
    if row.position:
        fine, _ = derive_position_tags(row.position)
        if fine:
            # Find or create position
            pos_res = await db.execute(
                select(Position).where(cast(ColumnElement[bool], Position.code == fine))
            )
            pos = pos_res.scalar_one_or_none()
            if not pos:
                parents = get_parents_for_fine(fine)
                pos = Position(code=fine, parents=parents)
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
