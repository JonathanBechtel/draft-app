"""Integration tests for the /draft-recap and /draft-recap/analysis pages.

Covers the pre-draft preview state (no results) and the populated state
(picks + consensus → steal/reach annotations), asserting 200 and key rendered
content for both routes.

Guard: integration tests require ``TEST_DATABASE_URL`` and ``PYTEST_ALLOW_DB=1``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.consensus import (
    BigBoardConsensus,
    ConsensusSnapshot,
    ConsensusTrigger,
)
from app.schemas.draft_results import DraftResult
from app.schemas.nba_teams import NbaTeam
from tests.integration.conftest import make_player

DRAFT_YEAR = 2026


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _seed_recap(db: AsyncSession) -> None:
    """Seed one consensus snapshot plus actual results with a clear steal/reach."""
    dal = NbaTeam(name="Dallas", abbreviation="DAL", slug="dal", primary_color="#00538C")
    phi = NbaTeam(name="Philly", abbreviation="PHI", slug="phi", primary_color="#006BB6")
    db.add_all([dal, phi])
    await db.flush()

    flagg = make_player("Cooper", "Flagg", school="Duke")
    edge = make_player("VJ", "Edgecombe", school="Baylor")
    reach = make_player("Reachy", "Guy", school="State")
    db.add_all([flagg, edge, reach])
    await db.flush()

    snap = ConsensusSnapshot(
        draft_year=DRAFT_YEAR,
        computed_at=_now(),
        num_boards=1,
        board_ids=[],
        trigger=ConsensusTrigger.MANUAL,
    )
    db.add(snap)
    await db.flush()
    assert snap.id is not None
    for player, rank, high, low in [(flagg, 1, 1, 2), (edge, 3, 2, 5), (reach, 12, 8, 15)]:
        assert player.id is not None
        db.add(
            BigBoardConsensus(
                snapshot_id=snap.id,
                draft_year=DRAFT_YEAR,
                player_id=player.id,
                consensus_rank=rank,
                avg_rank=float(rank),
                median_rank=float(rank),
                high_rank=high,
                low_rank=low,
                std_dev=0.0,
                num_sources=2,
            )
        )

    assert dal.id is not None and phi.id is not None
    picks = [
        (1, flagg, dal),  # in range
        (3, reach, phi),  # jumped (rank 12 band 8-15, drafted 3)
        (12, edge, dal),  # slid (rank 3 band 2-5, drafted 12)
    ]
    for pick, player, team in picks:
        assert player.id is not None
        db.add(
            DraftResult(
                draft_year=DRAFT_YEAR,
                overall_pick=pick,
                round=1,
                round_pick=pick,
                player_id=player.id,
                team_id=team.id,
                raw_player_name=player.display_name,
                raw_team=team.abbreviation,
                resolution_method="matched",
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_recap_preview_state(app_client: AsyncClient) -> None:
    """With no results the recap renders the live preview, not an error."""
    resp = await app_client.get("/draft-recap")
    assert resp.status_code == 200
    html = resp.text
    assert "Board vs. Reality" in html
    assert "Live tonight" in html


@pytest.mark.asyncio
async def test_recap_populated(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The single recap page renders board, movers, scatter and accuracy."""
    await _seed_recap(db_session)
    resp = await app_client.get("/draft-recap")
    assert resp.status_code == 200
    html = resp.text
    assert "Cooper Flagg" in html
    # Neutral framing — risers/fallers, not steal/reach.
    assert "Risers" in html
    assert "Fallers" in html
    assert "Biggest Riser" in html or "Biggest Faller" in html
    # Merged-in analysis sections.
    assert "Ranked vs. Drafted" in html  # face scatter
    assert "Which Boards Read the Room?" in html
    assert "VJ Edgecombe" in html
    # No value-laden language.
    assert "STEAL" not in html and "REACH" not in html


@pytest.mark.asyncio
async def test_analysis_url_redirects_to_recap(app_client: AsyncClient) -> None:
    """The old /draft-recap/analysis URL permanently redirects to the recap."""
    resp = await app_client.get("/draft-recap/analysis", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/draft-recap"


@pytest.mark.asyncio
async def test_year_specific_recap_route(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A per-year archive URL renders that year's recap."""
    await _seed_recap(db_session)
    resp = await app_client.get(f"/draft-recap/{DRAFT_YEAR}")
    assert resp.status_code == 200
    assert "Board vs. Reality" in resp.text
    assert "Cooper Flagg" in resp.text


@pytest.mark.asyncio
async def test_draft_hub_lists_board_and_recaps(
    app_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The /draft hub links the live board and the recap archive for each year."""
    await _seed_recap(db_session)
    resp = await app_client.get("/draft")
    assert resp.status_code == 200
    html = resp.text
    assert "Consensus Big Board" in html
    assert 'href="/consensus"' in html
    assert f'href="/draft-recap/{DRAFT_YEAR}"' in html


@pytest.mark.asyncio
async def test_draft_hub_preview_before_results(app_client: AsyncClient) -> None:
    """With no results the hub still shows the board and a recap teaser."""
    resp = await app_client.get("/draft")
    assert resp.status_code == 200
    assert "Consensus Big Board" in resp.text
    assert "goes live on draft night" in resp.text
