#!/usr/bin/env python
"""One-off ingest of the Prospects & Concepts 2026 pre-combine big board.

Reuses the live resolution cascade (``board_extraction_service.resolve_player``)
and ``board_service`` so the result is identical to the admin/auto-ingest path.

Dry run (default): resolve every name, print coverage, write nothing.
    scripts/with-db-env.sh conda run -n draftguru python \
        scripts/ingest_prospects_concepts_board.py

Execute against a target board id (clears its entries, then repopulates):
    DATABASE_URL=<prod> conda run -n draftguru python \
        scripts/ingest_prospects_concepts_board.py --execute --board-id 28
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import pkgutil
import sys
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOURCE_ID = 8
DRAFT_YEAR = 2026
PUBLISHED_AT = "2026-05-06"

# (rank, tier, name) — tier is column 2 of the pasted board.
BOARD: list[tuple[int, int, str]] = [
    (1, 1, "Cameron Boozer"),
    (2, 1, "Darryn Peterson"),
    (3, 1, "Caleb Wilson"),
    (4, 1, "AJ Dybantsa"),
    (5, 2, "Aday Mara"),
    (6, 2, "Kingston Flemings"),
    (7, 2, "Keaton Wagler"),
    (8, 2, "Mikel Brown Jr."),
    (9, 2, "Allen Graves"),
    (10, 2, "Bennett Stirtz"),
    (11, 2, "Yaxel Lendeborg"),
    (12, 2, "Darius Acuff Jr."),
    (13, 2, "Jayden Quaintance"),
    (14, 3, "Brayden Burries"),
    (15, 3, "Hannes Steinbach"),
    (16, 3, "Dailyn Swain"),
    (17, 3, "Tyler Tanner"),
    (18, 3, "Ebuka Okorie"),
    (19, 3, "Labaron Philon"),
    (20, 4, "C. Anderson Jr."),
    (21, 4, "Chris Cenac Jr."),
    (22, 4, "Karim Lopez"),
    (23, 4, "Joshua Jefferson"),
    (24, 4, "Tounde Yessoufou"),
    (25, 4, "Morez Johnson Jr."),
    (26, 4, "Henri Veesaar"),
    (27, 4, "Milan Momcilovic"),
    (28, 4, "Tarris Reed Jr."),
    (29, 4, "Amari Allen"),
    (30, 4, "Meleek Thomas"),
    (31, 5, "Alex Karaban"),
    (32, 5, "Bruce Thornton"),
    (33, 5, "Cameron Carr"),
    (34, 5, "Isaiah Evans"),
    (35, 5, "Ja'Kobi Gillespie"),
    (36, 5, "Koa Peat"),
    (37, 5, "Ugonna Onyenso"),
    (38, 5, "Braden Smith"),
    (39, 5, "Rueben Chinyelu"),
    (40, 5, "Nate Ament"),
    (41, 5, "Zuby Ejiofor"),
    (42, 6, "Richie Saunders"),
    (43, 6, "Maliq Brown"),
    (44, 6, "Izaiyah Nelson"),
    (45, 6, "Quadir Copeland"),
    (46, 6, "Baba Miller"),
    (47, 6, "Ryan Conwell"),
    (48, 6, "Duke Miles"),
    (49, 6, "Trevon Brazile"),
    (50, 6, "Emmanuel Sharp"),
    (51, 6, "Jeremy Fears Jr."),
    (52, 6, "Rafael Castro"),
    (53, 6, "Sergio de Larrea"),
    (54, 6, "Billy Richmond III"),
    (55, 6, "Otega Oweh"),
    (56, 6, "Luigi Suigo"),
    (57, 7, "Jacob Cofie"),
    (58, 7, "Nick Boyd"),
    (59, 7, "Darrion Williams"),
    (60, 7, "Robbie Avila"),
    (61, 7, "Tamin Lipsey"),
    (62, 7, "Malik Reneau"),
    (63, 7, "Tyler Nickel"),
    (64, 7, "Ernest Udeh Jr."),
    (65, 7, "Jaden Bradley"),
    (66, 7, "Jaxon Kohler"),
    (67, 7, "Jaylin Sellers"),
    (68, 7, "Trey Kaufman-Renn"),
    (69, 7, "Nate Bittle"),
    (70, 7, "Graham Ike"),
    (71, 7, "Melvin Council Jr."),
    (72, 7, "Nate Johnson"),
    (73, 7, "Lamar Wilkerson"),
    (74, 7, "Tobe Awaka"),
    (75, 7, "Milos Uzan"),
]


# High-confidence variant matches confirmed by name + school; resolved manually
# rather than guessed by the cascade (different spelling / abbreviated first name).
OVERRIDES: dict[str, int] = {
    "C. Anderson Jr.": 5382,  # -> Christian Anderson (Texas Tech)
    "Emmanuel Sharp": 6380,  # -> Emanuel Sharp (Houston)
}

# Real prospects absent from the prod DB; minted as stubs (is_stub=True) so all
# 75 ranks are filled. Enrich/merge later.
MINT_STUBS: set[str] = {
    "Ernest Udeh Jr.",
    "Jaxon Kohler",
    "Graham Ike",
    "Melvin Council Jr.",
}


def _load_all_schemas() -> None:
    import app.schemas as schemas_pkg

    for mod in pkgutil.iter_modules(schemas_pkg.__path__):
        importlib.import_module(f"app.schemas.{mod.name}")


async def _resolve_one(
    db: AsyncSession, rank: int, tier: int, name: str
) -> tuple[tuple, bool]:
    """Resolve a single board name into one plan row.

    Returns ``(row, blocked)`` where ``blocked`` marks a name the cascade could
    not resolve. Imports live here rather than at module scope because
    :func:`_load_all_schemas` must run before any schema module is importable.
    """
    from app.schemas.boards import ResolutionMethod
    from app.schemas.players_master import PlayerMaster
    from app.services.board_extraction_service import resolve_player

    if name in OVERRIDES:
        pid = OVERRIDES[name]
        dn = (
            await db.execute(
                select(PlayerMaster.display_name).where(  # type: ignore[call-overload]
                    PlayerMaster.id == pid  # type: ignore[arg-type]
                )
            )
        ).scalar_one_or_none()
        row = (
            rank,
            tier,
            name,
            pid,
            ResolutionMethod.MANUAL,
            f"MANUAL -> #{pid} {dn!r}",
        )
        return row, False

    if name in MINT_STUBS:
        return (rank, tier, name, None, ResolutionMethod.STUB, "STUB (mint new)"), False

    res = await resolve_player(db, name)
    if res.player_id is not None:
        row = (
            rank,
            tier,
            name,
            res.player_id,
            res.method,
            f"{res.method.value} -> #{res.player_id}",
        )
        return row, False

    unresolved = (
        rank,
        tier,
        name,
        None,
        ResolutionMethod.UNRESOLVED,
        "UNRESOLVED (unexpected)",
    )
    return unresolved, True


async def _build_plan(db: AsyncSession) -> tuple[list[tuple], int]:
    """Resolve every ``BOARD`` name into a write plan.

    Returns ``(plan, blocked)`` where each plan row is
    ``(rank, tier, name, player_id|None, method, note)``.
    """
    plan: list[tuple] = []
    blocked = 0
    for rank, tier, name in BOARD:
        row, is_blocked = await _resolve_one(db, rank, tier, name)
        plan.append(row)
        blocked += int(is_blocked)
    return plan, blocked


def _prepare_guarded_plan(
    plan: list[tuple],
    variant_index: Any,
    *,
    stub_method: Any,
    exact_method: Any,
    alias_method: Any,
) -> list[tuple]:
    """Apply identity-guard decisions before a board write can begin."""
    from app.services.player_identity_guard import resolve_variant_identity_match

    guarded_plan: list[tuple] = []
    for rank, tier, name, pid, method, note in plan:
        if method is stub_method:
            identity = resolve_variant_identity_match(
                name,
                variant_index.matches_for(name),
            )
            if identity.status in {"exact", "alias"}:
                if identity.player_id is None:
                    raise SystemExit(f"{name!r} matched without a canonical player id.")
                pid = identity.player_id
                method = exact_method if identity.status == "exact" else alias_method
                note = f"{method.value} -> #{pid} (reused existing player)"
            elif identity.status != "none":
                raise SystemExit(
                    f"{name!r} requires identity review ({identity.status}); "
                    "refusing to mint a duplicate stub."
                )
        guarded_plan.append((rank, tier, name, pid, method, note))
    return guarded_plan


async def _write_plan(
    db: AsyncSession, board_id: int, plan: list[tuple]
) -> tuple[int, int]:
    """Clear the target board and repopulate it from ``plan``.

    Returns ``(minted, reused)`` stub counts. Assumes the caller has already
    opened the write transaction.
    """
    from app.schemas.boards import Board, BoardEntry, BoardKind, ResolutionMethod
    from app.schemas.players_master import PlayerMaster
    from app.services.player_identity_guard import build_variant_identity_index

    board = (
        await db.execute(
            select(Board).where(Board.id == board_id)  # type: ignore[arg-type]
        )
    ).scalar_one()

    # Everything below deletes this board's entries, so confirm it is the board
    # this script is *for* -- not merely some pending board. A mistyped
    # --board-id would otherwise silently replace an unrelated board's contents,
    # and PENDING alone does not distinguish source, year, or even kind.
    expected = (BoardKind.BIG_BOARD, SOURCE_ID, DRAFT_YEAR, PUBLISHED_AT)
    actual = (
        board.kind,
        board.news_source_id,
        board.draft_year,
        board.published_at.date().isoformat(),
    )
    if board.status.value != "PENDING":
        raise SystemExit(f"board {board_id} is {board.status.value}, not PENDING")
    if actual != expected:
        raise SystemExit(
            f"board {board_id} identity mismatch; refusing to clear it.\n"
            f"  expected kind/source/year/published: {expected}\n"
            f"  actual:                              {actual}"
        )

    variant_index = await build_variant_identity_index(db)
    write_plan = _prepare_guarded_plan(
        plan,
        variant_index,
        stub_method=ResolutionMethod.STUB,
        exact_method=ResolutionMethod.EXACT,
        alias_method=ResolutionMethod.ALIAS,
    )

    # Clear existing entries only after every proposed stub passes the identity
    # guard, so a blocked execute cannot partially replace the board.
    await db.execute(
        delete(BoardEntry).where(
            BoardEntry.board_id == board_id  # type: ignore[arg-type]
        )
    )
    await db.flush()

    minted = 0
    reused = 0
    for rank, tier, name, pid, method, _note in write_plan:
        if method == ResolutionMethod.STUB:
            # Re-running --execute, or another workflow having added the name in
            # the meantime, must not mint a second canonical identity for the
            # same person. `display_name` is not unique, so an unconditional
            # insert silently succeeds and leaves duplicate players behind.
            existing = (
                (
                    await db.execute(
                        select(PlayerMaster.id).where(  # type: ignore[call-overload]
                            PlayerMaster.display_name == name
                        )
                    )
                )
                .scalars()
                .all()
            )
            if len(existing) > 1:
                raise SystemExit(
                    f"{name!r} already matches {len(existing)} players "
                    f"({sorted(existing)}); resolve the ambiguity before ingesting."
                )
            if existing:
                pid = existing[0]
                reused += 1
            else:
                stub = PlayerMaster(display_name=name, is_stub=True)
                db.add(stub)
                await db.flush()
                pid = stub.id
                if pid is None:
                    raise RuntimeError("Stub player insert did not populate player.id")
                minted += 1
                variant_index.add_display_name(pid, name)
        db.add(
            BoardEntry(
                board_id=board_id,
                player_id=pid,
                position=rank,
                raw_name=name,
                tier=tier,
                resolution_method=method,
            )
        )
    board.size = len(write_plan)
    db.add(board)
    return minted, reused


async def _run(args: argparse.Namespace) -> None:
    _load_all_schemas()

    # Normalize before handing the URL to the asyncpg dialect: a Neon-style URL
    # carries `sslmode`/`channel_binding`, which the driver rejects in that form,
    # so `--execute` against the documented prod string would fail on its first
    # query. Same helper the other production-facing scripts use.
    from app.utils.db_async import _prepare_asyncpg_connection

    normalized_url, connect_args = _prepare_asyncpg_connection(
        os.environ["DATABASE_URL"]
    )
    engine = create_async_engine(
        normalized_url, echo=False, pool_pre_ping=True, connect_args=connect_args
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as db:
        plan, blocked = await _build_plan(db)

        for rank, tier, name, _pid, _m, note in plan:
            print(f"  [{rank:>2}] T{tier} {name!r} -> {note}")
        print(f"\nplanned={len(plan)} blocked_unresolved={blocked} total={len(BOARD)}")

        if not args.execute:
            print("\n(dry run — no DB writes)")
            return

        if blocked:
            print("\nABORT: unexpected unresolved entries; refusing to --execute.")
            return

        # Close the implicit read transaction opened by the planning queries
        # so the write block can begin its own.
        await db.rollback()
        async with db.begin():
            minted, reused = await _write_plan(db, args.board_id, plan)

        print(
            f"\nDONE: board {args.board_id} -> {len(plan)} entries "
            f"({minted} stubs minted, {reused} existing reused)."
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--board-id", type=int, default=None)
    args = ap.parse_args()
    if args.execute and args.board_id is None:
        ap.error("--execute requires --board-id")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
