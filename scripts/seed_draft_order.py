#!/usr/bin/env python
r"""Seed the 2026 NBA draft order into draft_pick_slots.

The order below is the post-lottery 2026 draft order (all 60 picks, including
traded-pick reassignments), using DraftGuru's canonical team abbreviations
(see scripts/seed_nba_teams.py). Source: ESPN / Wikipedia draft-order pages,
cross-checked for the first round. Round-2 origins are best-effort.

Idempotent: replaces the whole year's order in one transaction via
``bulk_replace_draft_order``, so re-running yields the same final state.

Usage::

    scripts/with-db-env.sh conda run -n draftguru --no-capture-output \\
        python scripts/seed_draft_order.py
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.services.draft_order_service import PickSlotInput, bulk_replace_draft_order

load_dotenv()

DRAFT_YEAR = 2026

# (overall_pick, owner_abbreviation, original_team_abbreviation_or_None)
# Owner is the current owner of the pick; the original abbreviation is set when
# the pick was acquired via trade (rendered as a "via {abbr}" subscript).
# fmt: off
DRAFT_ORDER_2026: list[tuple[int, str, str | None]] = [
    # ---- Round 1 ----
    (1,  "WAS", None),
    (2,  "UTA", None),
    (3,  "MEM", None),
    (4,  "CHI", None),
    (5,  "LAC", "IND"),
    (6,  "BKN", None),
    (7,  "SAC", None),
    (8,  "ATL", "NOP"),
    (9,  "DAL", None),
    (10, "MIL", None),
    (11, "GSW", None),
    (12, "OKC", "LAC"),
    (13, "MIA", None),
    (14, "CHA", None),
    (15, "CHI", "POR"),
    (16, "MEM", "PHX"),
    (17, "OKC", "PHI"),
    (18, "CHA", "ORL"),
    (19, "TOR", None),
    (20, "SAS", "ATL"),
    (21, "DET", "MIN"),
    (22, "PHI", "HOU"),
    (23, "ATL", "CLE"),
    (24, "NYK", None),
    (25, "LAL", None),
    (26, "DEN", None),
    (27, "BOS", None),
    (28, "MIN", "DET"),
    (29, "CLE", "SAS"),
    (30, "DAL", "OKC"),
    # ---- Round 2 ----
    (31, "NYK", "WAS"),
    (32, "MEM", "IND"),
    (33, "BKN", None),
    (34, "SAC", None),
    (35, "SAS", "UTA"),
    (36, "LAC", "MEM"),
    (37, "OKC", "DAL"),
    (38, "CHI", "NOP"),
    (39, "HOU", "CHI"),
    (40, "BOS", "MIL"),
    (41, "MIA", "GSW"),
    (42, "SAS", "POR"),
    (43, "BKN", "LAC"),
    (44, "SAS", "MIA"),
    (45, "SAC", "CHA"),
    (46, "ORL", None),
    (47, "PHX", "PHI"),
    (48, "DAL", "PHX"),
    (49, "DEN", "ATL"),
    (50, "TOR", None),
    (51, "WAS", "MIN"),
    (52, "LAC", "CLE"),
    (53, "HOU", None),
    (54, "GSW", "LAL"),
    (55, "NYK", None),
    (56, "CHI", "DEN"),
    (57, "ATL", "BOS"),
    (58, "NOP", "DET"),
    (59, "MIN", "SAS"),
    (60, "WAS", "OKC"),
]
# fmt: on


async def seed_draft_order() -> None:
    """Resolve team abbreviations to ids and replace the 2026 pick order."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    engine = create_async_engine(db_url)
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    from app.schemas.nba_teams import NbaTeam

    async with session_factory() as session:
        teams = (await session.execute(select(NbaTeam))).scalars().all()
        id_by_abbr = {t.abbreviation: t.id for t in teams if t.id is not None}

        # Validate every referenced abbreviation resolves before writing.
        referenced = {abbr for _, abbr, _ in DRAFT_ORDER_2026}
        referenced |= {orig for _, _, orig in DRAFT_ORDER_2026 if orig is not None}
        missing = sorted(a for a in referenced if a not in id_by_abbr)
        if missing:
            print(
                "ERROR: these abbreviations are not in nba_teams "
                f"(seed teams first?): {', '.join(missing)}"
            )
            sys.exit(1)

        slots: list[PickSlotInput] = []
        for overall_pick, owner_abbr, orig_abbr in DRAFT_ORDER_2026:
            round_no = 1 if overall_pick <= 30 else 2
            round_pick = overall_pick if round_no == 1 else overall_pick - 30
            slots.append(
                PickSlotInput(
                    overall_pick=overall_pick,
                    round=round_no,
                    round_pick=round_pick,
                    team_id=id_by_abbr[owner_abbr],
                    original_team_id=(
                        id_by_abbr[orig_abbr] if orig_abbr is not None else None
                    ),
                    trade_note=(f"via {orig_abbr}" if orig_abbr is not None else None),
                )
            )

        count = await bulk_replace_draft_order(
            session, draft_year=DRAFT_YEAR, slots=slots
        )
        await session.commit()

    await engine.dispose()
    print(f"Done: seeded {count} pick slots for {DRAFT_YEAR}.")


if __name__ == "__main__":
    asyncio.run(seed_draft_order())
