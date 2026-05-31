"""Repair player records contaminated by same-surname namesakes.

A previous version of ``scripts/ingest_player_bios.py`` matched a scraped
Basketball-Reference bio onto an existing player using a *first-initial*
fallback (e.g. "Derek" Harper matched the "Dylan" Harper record). When the
target record was a near-empty stub, the namesake's bio fully overwrote it;
in every case the namesake's BBRef external id, alias, bio snapshot, social
handle, and college-stats seasons were attached to the wrong ``player_id``.

This script repairs the damage using only cached data (no network):

1. Identify candidates: players whose attached ``bbr`` snapshots include a
   name that does not match their ``display_name``.
2. For each candidate, resolve the *correct* BBRef id as the one whose
   snapshot ``<h1>`` name matches ``display_name``.
3. Purge the foreign namesakes' external ids, aliases, snapshots, and social
   handles; re-derive the bio fields from the correct snapshot; delete the
   contaminated college-stats rows (they are re-scraped afterward from the
   single correct BBRef page via ``scripts/scrape_college_stats.py``).

Only the unambiguous contamination is auto-fixed; everything else is flagged
for manual review and left untouched (we never guess across namesakes):
  * ``resolvable``   — >=2 distinct bbr ids attached and exactly one whose
                       snapshot name matches ``display_name`` exactly -> auto-fix.
  * ``ambiguous``    — >=2 bbr ids match ``display_name`` exactly. Flag only.
  * ``near_match``   — no exact match, but exactly one suffix/variant match.
                       Flag with a suggestion (Jr./Sr. cannot be auto-trusted).
  * ``no_match``     — no attached bbr snapshot name matches. Flag only.
  * ``name_variant`` — a single attached bbr id whose name differs from
                       ``display_name`` (dropped suffix or nickname, or the
                       wrong person); indistinguishable here. Flag only.
  * ``clean``        — single bbr id whose name matches. Skipped.

Usage (report only, no writes)::

    scripts/with-db-env.sh conda run -n draftguru python scripts/fix_namesake_contamination.py

Apply repairs to resolvable records, then re-scrape the now single-id college
stats::

    scripts/with-db-env.sh conda run -n draftguru python scripts/fix_namesake_contamination.py --apply
    scripts/with-db-env.sh conda run -n draftguru python scripts/scrape_college_stats.py --only-missing --verbose
"""

import argparse
import asyncio
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import delete, select

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.bbref_bio_scraper import PlayerBio, parse_player_html  # noqa: E402
from scripts.ingest_player_bios import _norm, _update_master  # noqa: E402
from app.schemas.player_aliases import PlayerAlias  # noqa: E402
from app.schemas.player_bio_snapshots import PlayerBioSnapshot  # noqa: E402
from app.schemas.player_college_stats import PlayerCollegeStats  # noqa: E402
from app.schemas.player_external_ids import PlayerExternalId  # noqa: E402
from app.schemas.players_master import PlayerMaster  # noqa: E402
from app.utils.db_async import SessionLocal  # noqa: E402

SYSTEM_BBR = "bbr"
SYSTEM_INSTAGRAM = "instagram"
SYSTEM_X = "x"

# Bio fields populated by the bbr bio ingest (see _update_master). These are
# the only players_master columns this repair touches; identity columns
# (display_name/first/last/middle/suffix) are trusted and never modified.
BIO_FIELDS = [
    "birthdate",
    "nba_debut_date",
    "birth_city",
    "birth_state_province",
    "birth_country",
    "shoots",
    "school",
    "school_raw",
    "high_school",
    "draft_year",
    "draft_round",
    "draft_pick",
    "draft_team",
    "nba_debut_season",
]

_SLUG_RE = re.compile(r"/players/[a-z]/([a-z0-9]+)\.html")


def _slug_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = _SLUG_RE.search(url)
    return m.group(1) if m else None


@dataclass
class PlayerCase:
    player_id: int
    display_name: str
    classification: str  # resolvable | no_match | ambiguous | clean
    correct_slug: Optional[str]
    correct_name: Optional[str]
    wrong_slugs: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "player_id": self.player_id,
            "display_name": self.display_name,
            "classification": self.classification,
            "correct_slug": self.correct_slug,
            "correct_name": self.correct_name,
            "wrong_slugs": self.wrong_slugs,
            "notes": self.notes,
            "actions": self.actions,
        }


async def _load_state(
    db,
) -> Tuple[
    Dict[int, PlayerMaster],
    Dict[int, List[PlayerExternalId]],
    Dict[int, List[PlayerBioSnapshot]],
]:
    """Load players plus their bbr external ids and bbr snapshots."""
    players: Dict[int, PlayerMaster] = {}
    for p in (await db.execute(select(PlayerMaster))).scalars().all():
        if p.id is not None:
            players[p.id] = p

    ext_by_player: Dict[int, List[PlayerExternalId]] = defaultdict(list)
    ext_rows = (
        (
            await db.execute(
                select(PlayerExternalId).where(PlayerExternalId.system == SYSTEM_BBR)  # type: ignore[arg-type]
            )
        )
        .scalars()
        .all()
    )
    for e in ext_rows:
        ext_by_player[e.player_id].append(e)

    snaps_by_player: Dict[int, List[PlayerBioSnapshot]] = defaultdict(list)
    snap_rows = (
        (
            await db.execute(
                select(PlayerBioSnapshot).where(PlayerBioSnapshot.source == SYSTEM_BBR)  # type: ignore[arg-type]
            )
        )
        .scalars()
        .all()
    )
    for s in snap_rows:
        snaps_by_player[s.player_id].append(s)

    return players, ext_by_player, snaps_by_player


def _bio_by_slug(
    snaps: List[PlayerBioSnapshot],
) -> Dict[str, PlayerBio]:
    """Parse each bbr snapshot into a PlayerBio keyed by its slug.

    When a slug has multiple snapshots (re-scrapes) the latest is used.
    """
    chosen: Dict[str, PlayerBioSnapshot] = {}
    for s in snaps:
        slug = _slug_from_url(s.source_url)
        if not slug or not s.raw_meta_html:
            continue
        prev = chosen.get(slug)
        if prev is None or (
            s.scraped_at and prev.scraped_at and s.scraped_at >= prev.scraped_at
        ):
            chosen[slug] = s
    out: Dict[str, PlayerBio] = {}
    for slug, snap in chosen.items():
        try:
            out[slug] = parse_player_html(
                slug[:1], slug, snap.raw_meta_html or "", snap.source_url or ""
            )
        except Exception:
            continue
    return out


_SUFFIX_RE = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def _norm_nosuffix(name: str) -> str:
    """Normalize a name and drop generational suffixes (Jr/Sr/II..V)."""
    return re.sub(r"\s+", " ", _SUFFIX_RE.sub("", _norm(name))).strip()


def classify(
    player: PlayerMaster,
    ext_ids: List[PlayerExternalId],
    bios: Dict[str, PlayerBio],
) -> PlayerCase:
    """Classify a record's contamination.

    Only the unambiguous case is auto-resolved: >=2 distinct BBRef ids
    attached, with exactly one whose snapshot name matches ``display_name``
    exactly. Every other case is flagged and never mutated, because
    suffix/nickname/father-son variants cannot be told apart from the cached
    snapshots alone (e.g. "Tim Hardaway" attached to "Tim Hardaway Jr." looks
    identical whether it is a dropped suffix or the wrong person).
    """
    pid = player.id
    assert pid is not None
    name_norm = _norm(player.display_name or "")
    name_ns = _norm_nosuffix(player.display_name or "")
    slugs = sorted({e.external_id for e in ext_ids})

    exact = [s for s in slugs if s in bios and _norm(bios[s].full_name) == name_norm]
    wrong = [s for s in slugs if s in bios and _norm(bios[s].full_name) != name_norm]
    near = [s for s in wrong if _norm_nosuffix(bios[s].full_name) == name_ns]
    unknown = [s for s in slugs if s not in bios]

    case = PlayerCase(
        player_id=pid,
        display_name=player.display_name or "",
        classification="clean",
        correct_slug=None,
        correct_name=None,
        wrong_slugs=wrong,
    )
    if unknown:
        case.notes.append(f"no_snapshot_for={unknown}")

    distinct_ids = len(slugs)

    # Only treat >=2 distinct attached BBRef ids as the namesake-merge bug.
    if distinct_ids < 2:
        if exact:
            case.classification = "clean"
        else:
            # A single attached page whose name is a variant of display_name.
            # Could be a harmless dropped suffix/nickname OR the wrong person
            # (father vs. son). Indistinguishable here -> flag, never mutate.
            single = slugs[0] if slugs else None
            case.classification = "name_variant"
            case.correct_name = bios.get(single).full_name if single in bios else None  # type: ignore[union-attr]
            case.notes.append("single bbr id; snapshot name differs from display_name")
        return case

    if len(exact) == 1:
        case.correct_slug = exact[0]
        case.correct_name = bios[exact[0]].full_name
        case.classification = "resolvable"
        # Purge every other attached id, including any whose snapshot is
        # missing ("unknown"). The exact display_name match is authoritative,
        # and leaving an unverified id attached would let the follow-up college
        # rescrape re-pull a namesake's stats via _find_eligible_players (which
        # iterates every bbr id still on the player).
        if unknown:
            case.wrong_slugs = sorted(set(case.wrong_slugs) | set(unknown))
            case.notes.append(f"purging unknown-snapshot ids: {unknown}")
    elif len(exact) > 1:
        case.classification = "ambiguous"
        case.correct_name = ", ".join(bios[s].full_name for s in exact)
        case.notes.append(f"multiple exact name matches: {exact}")
    elif len(near) == 1:
        case.classification = "near_match"
        case.correct_name = bios[near[0]].full_name
        case.notes.append(f"suggested (suffix/variant) match: {near[0]} -> review")
    else:
        case.classification = "no_match"
        case.notes.append("no bbr snapshot name matches display_name")
    return case


async def repair(
    db,
    player: PlayerMaster,
    case: PlayerCase,
    ext_ids: List[PlayerExternalId],
    snaps: List[PlayerBioSnapshot],
    bios: Dict[str, PlayerBio],
    apply: bool,
) -> None:
    pid = case.player_id

    if case.classification == "resolvable":
        correct_slug = case.correct_slug
        assert correct_slug is not None
        correct_bio = bios[correct_slug]
        wrong_bios = [bios[s] for s in case.wrong_slugs if s in bios]

        # 1) Reset the bbr-managed bio fields and re-derive them from the
        #    correct snapshot. Clearing first guarantees no namesake residue
        #    survives in a field the correct page happens to leave blank; the
        #    correct snapshot is the authoritative source for these columns.
        for attr in BIO_FIELDS:
            if getattr(player, attr) is not None:
                setattr(player, attr, None)
        # correct_bio is a PlayerBio; _update_master reads the same attributes
        # that BioRow exposes, so the structural mismatch is safe here.
        await _update_master(db, player, correct_bio, overwrite=True)  # type: ignore[arg-type]
        case.actions.append(f"reset + re-derived bio from {correct_slug}")

        # 3) Purge foreign bbr external ids.
        if case.wrong_slugs:
            case.actions.append(f"delete bbr ext_ids {case.wrong_slugs}")
            if apply:
                await db.execute(
                    delete(PlayerExternalId).where(
                        PlayerExternalId.player_id == pid,  # type: ignore[arg-type]
                        PlayerExternalId.system == SYSTEM_BBR,  # type: ignore[arg-type]
                        PlayerExternalId.external_id.in_(case.wrong_slugs),  # type: ignore[attr-defined]
                    )
                )

        # 4) Purge foreign bbr snapshots.
        wrong_snap_ids = [
            s.id
            for s in snaps
            if _slug_from_url(s.source_url) in case.wrong_slugs and s.id is not None
        ]
        if wrong_snap_ids:
            case.actions.append(f"delete {len(wrong_snap_ids)} bbr snapshots")
            if apply:
                await db.execute(
                    delete(PlayerBioSnapshot).where(
                        PlayerBioSnapshot.id.in_(wrong_snap_ids)  # type: ignore[union-attr]
                    )
                )

        # 5) Purge namesake social handles. The correct snapshot's social
        #    handles are kept; any social handle attached that belongs only to
        #    a namesake snapshot is removed.
        correct_socials = {
            h
            for h in [correct_bio.social_instagram_handle, correct_bio.social_x_handle]
            if h
        }
        wrong_socials = {
            h
            for wb in wrong_bios
            for h in [wb.social_instagram_handle, wb.social_x_handle]
            if h and h not in correct_socials
        }
        if wrong_socials:
            case.actions.append(f"delete socials {sorted(wrong_socials)}")
            if apply:
                await db.execute(
                    delete(PlayerExternalId).where(
                        PlayerExternalId.player_id == pid,  # type: ignore[arg-type]
                        PlayerExternalId.system.in_([SYSTEM_INSTAGRAM, SYSTEM_X]),  # type: ignore[attr-defined]
                        PlayerExternalId.external_id.in_(sorted(wrong_socials)),  # type: ignore[attr-defined]
                    )
                )

        # 6) Purge namesake aliases (bbr-context aliases not matching the name).
        name_norm = _norm(case.display_name)
        await _purge_bad_aliases(db, pid, name_norm, case, apply)

        # 7) Drop all college rows; they are re-scraped from the single
        #    remaining (correct) bbr id afterward.
        await _delete_college(db, pid, case, apply)

    # All other classifications (no_match / near_match / ambiguous /
    # name_variant) are reported only. They cannot be repaired safely from the
    # cached snapshots — the correct BBRef page is either not attached or
    # cannot be told apart from a same-name/relative namesake — so they are
    # left untouched for manual remapping rather than guessed at.


async def _purge_bad_aliases(
    db, pid: int, name_norm: str, case: PlayerCase, apply: bool
) -> None:
    stmt = select(PlayerAlias).where(PlayerAlias.player_id == pid)  # type: ignore[arg-type]
    alias_rows = (await db.execute(stmt)).scalars().all()
    bad = [
        a.id
        for a in alias_rows
        if a.context == SYSTEM_BBR
        and _norm(a.full_name) != name_norm
        and a.id is not None
    ]
    if bad:
        case.actions.append(f"delete {len(bad)} namesake aliases")
        if apply:
            await db.execute(delete(PlayerAlias).where(PlayerAlias.id.in_(bad)))  # type: ignore[union-attr]


async def _delete_college(db, pid: int, case: PlayerCase, apply: bool) -> None:
    count = (
        await db.execute(
            select(PlayerCollegeStats.id).where(PlayerCollegeStats.player_id == pid)  # type: ignore[call-overload, arg-type]
        )
    ).all()
    if count:
        case.actions.append(f"delete {len(count)} college rows (re-scrape needed)")
        if apply:
            await db.execute(
                delete(PlayerCollegeStats).where(PlayerCollegeStats.player_id == pid)  # type: ignore[arg-type]
            )


async def run(
    apply: bool, report_path: Optional[Path], only_player_id: Optional[int]
) -> int:
    async with SessionLocal() as db:
        players, ext_by_player, snaps_by_player = await _load_state(db)

        cases: List[PlayerCase] = []
        for pid, ext_ids in ext_by_player.items():
            if only_player_id is not None and pid != only_player_id:
                continue
            player = players.get(pid)
            if player is None:
                continue
            bios = _bio_by_slug(snaps_by_player.get(pid, []))
            case = classify(player, ext_ids, bios)
            if case.classification == "clean":
                continue
            cases.append(case)
            await repair(
                db, player, case, ext_ids, snaps_by_player.get(pid, []), bios, apply
            )

        if apply:
            await db.commit()

        # --- report ---
        by_class: Dict[str, int] = defaultdict(int)
        for c in cases:
            by_class[c.classification] += 1
        # Only resolvable records have their college rows deleted/re-derived,
        # so only they need a college re-scrape afterward.
        rescrape_ids = sorted(
            c.player_id for c in cases if c.classification == "resolvable"
        )

        print(f"{'APPLIED' if apply else 'DRY-RUN'} — {len(cases)} flagged records")
        for k in ("resolvable", "near_match", "no_match", "ambiguous", "name_variant"):
            print(f"  {k}: {by_class.get(k, 0)}")
        print(f"  auto-fixed (resolvable) need college re-scrape: {len(rescrape_ids)}")

        flagged = [
            c
            for c in cases
            if c.classification
            in ("near_match", "no_match", "ambiguous", "name_variant")
        ]
        if flagged:
            print(f"\nFLAGGED FOR MANUAL REVIEW ({len(flagged)} — not modified):")
            for c in flagged:
                print(
                    f"  [{c.classification}] {c.player_id} {c.display_name} — {'; '.join(c.notes)}"
                )

        if report_path:
            report_path.write_text(
                json.dumps(
                    {
                        "applied": apply,
                        "summary": dict(by_class),
                        "rescrape_player_ids": rescrape_ids,
                        "cases": [c.to_dict() for c in cases],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"\nFull report written to {report_path}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--apply", action="store_true", help="Write changes (default: dry-run)"
    )
    ap.add_argument(
        "--player-id", type=int, default=None, help="Restrict to one player id"
    )
    ap.add_argument(
        "--report",
        type=Path,
        default=REPO_ROOT / "scripts" / "data" / "namesake_contamination_report.json",
        help="Path to write the JSON report",
    )
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args.apply, args.report, args.player_id)))


if __name__ == "__main__":
    main()
