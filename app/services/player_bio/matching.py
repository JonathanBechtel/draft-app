"""Resolve a scraped bio row to a canonical ``players_master`` id.

The lookup tables are loaded once per ingest run and then consulted in
descending order of confidence (external id, exact alias, deterministic
first+last match). Ambiguity resolves to *no match*, never to a guess -- see
``_deterministic_match``.
"""

import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.schemas.player_aliases import PlayerAlias
from app.schemas.player_external_ids import PlayerExternalId
from app.schemas.players_master import PlayerMaster

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
    # Last exact + first exact only. We deliberately do NOT fall back to a
    # first-initial match: distinct first names that share an initial (e.g.
    # "Derek" vs "Dylan" Harper) are different people, and guessing across
    # them silently merges one player's bio/stats onto another. Ambiguous
    # names must resolve to "unmatched" (create a new record / leave for
    # manual review) rather than be assigned to a same-initial namesake.
    for pid in candidates:
        p = pm_by_id.get(pid)
        if p and p.first_name and _norm(p.first_name) == _norm(first_name or ""):
            return pid
    return None
