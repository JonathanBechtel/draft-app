"""Fix issue #495: recover top-pick SL logs stranded by duplicate master rows.

Each target's SL logs are stranded because a phantom duplicate master row
(suffix/diacritic variant) made resolution ambiguous, so the logs sit either in a
PENDING review or resolved onto the wrong (dup) row. Per target we:
  1. link the SL source player -> draft-info row, backfilling its game logs
     (moves logs onto the correct player even if currently on a dup),
  2. mark any PENDING review APPROVED,
  3. merge the now-empty phantom dup stub(s) into the draft-info row,
then rebuild the materialized SL seasons so the players surface in the Explorer.

merge_players() does NOT reassign summer_league_* tables, so a discard is only
merged AFTER its SL logs have been moved off it (step 1); a safety check refuses
to merge any row that still holds SL references.

Fully NAME-DRIVEN: every players_master id is resolved per-branch at runtime, so
this runs unchanged against dev and prod (their ids differ).

Dry-run by default; pass --apply to mutate. --url-env picks the target branch.
"""

from __future__ import annotations

import argparse
import asyncio
import os

from dotenv import load_dotenv
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeaguePlayerResolutionReview,
    SummerLeagueResolutionStatus,
    SummerLeagueReviewStatus,
    SummerLeagueSourceRecord,
)
from app.services.player_merge_service import merge_players, preview_merge
from app.services.sources.summer_league.metrics import rebuild
from app.services.backbone.player_resolution import (
    _backfill_participation_and_affiliation,
    _backfill_player_game_logs,
    _backfill_shot_events,
    _confirm_resolution,
    _find_external_id_player,
    record_resolution_review_decision,
)
from app.utils.db_async import _prepare_asyncpg_connection

load_dotenv()

# source = SL source_players.raw_player_name; keep = canonical draft-info
# display_name; discards = phantom dup master display_names to merge into keep.
TARGETS: list[tuple[str, str, list[str]]] = [
    ("Wendell Carter Jr.", "Wendell Carter Jr.", ["Wendell Carter"]),
    ("Dereck Lively II", "Dereck Lively II", ["Dereck Lively Jr."]),
    ("Tidjane Salaün", "Tidjane Salaün", ["Tidjane Salaun"]),
    ("VJ Edgecombe", "VJ Edgecombe", ["VJ Edgecombe Jr."]),
    ("Ronald Holland II", "Ron Holland II", []),
    ("P.J. Washington", "PJ Washington", ["P.J. Washington"]),
]


async def _drafted_id(db, display_name: str) -> int:
    rows = (
        await db.execute(
            select(PlayerMaster.id, PlayerMaster.draft_year).where(  # type: ignore[call-overload]
                PlayerMaster.display_name == display_name
            )
        )
    ).all()
    drafted = [r.id for r in rows if r.draft_year is not None]
    if len(drafted) != 1:
        raise SystemExit(
            f"  ! {display_name!r}: expected exactly 1 drafted row, got {rows}"
        )
    return int(drafted[0])


async def _plain_id(db, display_name: str) -> int | None:
    r = (
        await db.execute(
            select(PlayerMaster.id).where(PlayerMaster.display_name == display_name)  # type: ignore[call-overload]
        )
    ).first()
    return int(r.id) if r else None


async def _sl_refs(db, player_id: int) -> int:
    logs = (
        await db.execute(
            text(
                "SELECT COUNT(*) FROM summer_league_player_game_logs WHERE player_id=:p"
            ),
            {"p": player_id},
        )
    ).scalar()
    srcs = (
        await db.execute(
            text(
                "SELECT COUNT(*) FROM summer_league_source_players WHERE canonical_player_id=:p"
            ),
            {"p": player_id},
        )
    ).scalar()
    return int(logs or 0) + int(srcs or 0)


async def _reassign_sl_refs(db, *, discard_id: int, keep_id: int) -> None:
    """Move any residual Summer League references off a discard onto the keep row.

    merge_players() does not touch summer_league_* tables, and their FKs are
    RESTRICT, so a discard must hold zero SL refs before it can be deleted.
    """
    for stmt in (
        "UPDATE summer_league_player_game_logs SET player_id=:k WHERE player_id=:d",
        "UPDATE summer_league_source_players SET canonical_player_id=:k WHERE canonical_player_id=:d",
        "UPDATE summer_league_participation SET player_id=:k WHERE player_id=:d",
        "UPDATE summer_league_shot_events SET player_id=:k WHERE player_id=:d",
    ):
        await db.execute(text(stmt), {"k": keep_id, "d": discard_id})


async def _seasons(db, player_id: int) -> int:
    return int(
        (
            await db.execute(
                text(
                    "SELECT COUNT(*) FROM summer_league_player_seasons WHERE player_id=:p"
                ),
                {"p": player_id},
            )
        ).scalar()
        or 0
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url-env", default="DATABASE_URL")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    # Use the app helper so libpq-only query args (sslmode, channel_binding) on
    # repo-standard Neon URLs are stripped/mapped for asyncpg.
    normalized_url, connect_args = _prepare_asyncpg_connection(os.environ[args.url_env])
    branch = normalized_url.split("@")[-1].split(".")[0]
    print(
        f"\n=== fix #495 [{'APPLY' if args.apply else 'DRY-RUN'}] {args.url_env} ({branch}) ===\n"
    )

    engine = create_async_engine(normalized_url, connect_args=connect_args)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        resolved = []
        for source_name, keep_name, discards in TARGETS:
            sp = (
                await db.execute(
                    select(SummerLeagueSourceRecord).where(  # type: ignore[call-overload]
                        SummerLeagueSourceRecord.raw_player_name == source_name  # type: ignore[arg-type]
                    )
                )
            ).scalar_one_or_none()
            if sp is None:
                print(f"  SKIP {source_name!r}: no source player on this branch")
                continue
            keep_id = await _drafted_id(db, keep_name)
            discard_ids = []
            for d in discards:
                did = await _plain_id(db, d)
                if did is None:
                    print(f"    note: discard {d!r} absent on this branch")
                elif did == keep_id:
                    print(f"    note: discard {d!r} IS the keep row — skipping merge")
                else:
                    discard_ids.append((d, did))
            review = (
                (
                    await db.execute(
                        select(SummerLeaguePlayerResolutionReview)
                        .where(
                            SummerLeaguePlayerResolutionReview.source_player_id == sp.id  # type: ignore[arg-type]
                        )
                        .order_by(SummerLeaguePlayerResolutionReview.id.desc())  # type: ignore[union-attr]
                    )
                )
                .scalars()
                .first()
            )
            logs = (
                await db.execute(
                    text(
                        "SELECT COUNT(*) FROM summer_league_player_game_logs WHERE source_player_id=:s"
                    ),
                    {"s": sp.id},
                )
            ).scalar()
            print(
                f"  {keep_name:22} sp={sp.id} logs={logs} -> keep id={keep_id}; "
                f"merge dups {[d[1] for d in discard_ids]}; review={review.status if review else None}"
            )
            # Store plain ids: the ORM objects are expired by the rollback below,
            # and re-fetching fresh inside the write txn avoids sync lazy-loads.
            resolved.append(
                (sp.id, keep_id, discard_ids, review.id if review else None)
            )

        if not args.apply:
            print("\n(dry-run — pass --apply to execute)")
            await engine.dispose()
            return

        # Close the read-only autobegun transaction before opening a write one.
        await db.rollback()
        async with db.begin():
            for sp_id, keep_id, discard_ids, review_id in resolved:
                sp = await db.get(SummerLeagueSourceRecord, sp_id)
                assert sp is not None  # planned above; still present in this txn
                existing_ext = await _find_external_id_player(
                    db, sp.nba_stats_person_id
                )
                if existing_ext is not None and existing_ext != keep_id:
                    # The external id must belong to a dup we're about to merge,
                    # so the merge sweeps player_external_ids onto the survivor.
                    # Otherwise we'd leave a stale external-id owner and a later
                    # resolver run (which trusts the external id first) would flip
                    # the source back to it.
                    if existing_ext not in {d[1] for d in discard_ids}:
                        raise SystemExit(
                            f"  ! sp={sp_id}: NBA external id owned by player "
                            f"{existing_ext}, which is not among the merged dups "
                            f"{[d[1] for d in discard_ids]} — refusing to leave a "
                            f"stale external-id owner"
                        )
                    # Already resolved to a dup (external id points elsewhere).
                    # Reassign SL rows via the same helpers _confirm_resolution
                    # uses, minus external-id creation; the dup merge below sweeps
                    # the external id onto the survivor.
                    n = await _backfill_player_game_logs(
                        db, source_player_id=sp_id, player_id=keep_id
                    )
                    await _backfill_participation_and_affiliation(
                        db, source_player_id=sp_id, player_id=keep_id
                    )
                    await _backfill_shot_events(
                        db, source_player_id=sp_id, player_id=keep_id
                    )
                    sp.canonical_player_id = keep_id
                    sp.resolution_status = SummerLeagueResolutionStatus.MANUAL
                    await db.flush()
                    print(
                        f"    re-pointed sp={sp_id} (ext id from {existing_ext}) -> {keep_id}: {n} logs"
                    )
                else:
                    res = await _confirm_resolution(
                        db,
                        sp,
                        player_id=keep_id,
                        status=SummerLeagueResolutionStatus.MANUAL,
                        method="manual_review_495",
                    )
                    print(
                        f"    linked sp={sp_id} -> {keep_id}: {res.logs_backfilled} logs backfilled"
                    )
                if review_id is not None:
                    await record_resolution_review_decision(
                        db,
                        review_id=review_id,
                        status=SummerLeagueReviewStatus.APPROVED,
                        selected_player_id=keep_id,
                        review_note="issue #495: top-pick orphan, dup-blocked",
                    )
                for dname, did in discard_ids:
                    # Sweep any residual SL refs off the dup first (merge_players
                    # can't), then confirm it is safe to delete.
                    await _reassign_sl_refs(db, discard_id=did, keep_id=keep_id)
                    refs = await _sl_refs(db, did)
                    if refs:
                        raise SystemExit(
                            f"  ! refusing to merge {dname!r} (id={did}): still holds {refs} SL refs"
                        )
                    rep = await preview_merge(db, keep_id=keep_id, discard_id=did)
                    n = sum(v.get("reassigned", 0) for v in rep.per_table.values())
                    await merge_players(db, keep_id=keep_id, discard_id=did)
                    print(
                        f"    merged dup {dname!r} id={did} -> {keep_id} ({n} rows reassigned)"
                    )

        print("\n  rebuilding SL metrics ...")
        async with db.begin():
            summary = await rebuild(db)
        print(
            f"  rebuilt: {summary['seasons']} seasons, {summary['contexts']} contexts"
        )

        print("\n  verification (seasons per fixed player):")
        for _sp_id, keep_id, _d, _r in resolved:
            print(f"    keep id={keep_id}  seasons={await _seasons(db, keep_id)}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
