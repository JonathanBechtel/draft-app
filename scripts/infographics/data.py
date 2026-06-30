"""Load /draft-recap data straight from the app services.

Reuses ``get_draft_recap`` + ``split_movers`` so the numbers are byte-identical
to the live page, then returns a plain dict the templates render without any
further DB access. (Phase 3 will add a ``from_gather_json`` adapter so the same
templates can render off an x_threads ``gather.json``.)
"""

from __future__ import annotations

import asyncio
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from app.services.draft_results_service import (  # noqa: E402
    get_draft_recap,
    get_recap_years,
    split_movers,
)
from app.utils.db_async import SessionLocal, load_schema_modules  # noqa: E402

# Fallback when no year has results yet (matches app/routes/ui.py).
CONSENSUS_DRAFT_YEAR = 2026


async def _load(draft_year: Optional[int]) -> dict:
    load_schema_modules()
    async with SessionLocal() as db:
        years = await get_recap_years(db)
        year = draft_year or (years[0] if years else CONSENSUS_DRAFT_YEAR)
        picks, _summary = await get_draft_recap(db, draft_year=year)
        later, earlier = split_movers(picks, limit=10)
    return {
        "draft_year": year,
        "picks": [p.model_dump() for p in picks],
        "biggest_risers": [
            p.model_dump() for p in earlier
        ],  # drafted earlier than consensus
        "biggest_fallers": [
            p.model_dump() for p in later
        ],  # drafted later than consensus
    }


def load_recap(draft_year: Optional[int] = None) -> dict:
    """Synchronous entry point for the render CLI."""
    return asyncio.run(_load(draft_year))
