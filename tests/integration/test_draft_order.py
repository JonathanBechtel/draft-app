"""Integration tests for the draft-order reference and mock-draft overlay.

Covers:
- ``DraftPickSlot`` uniqueness constraints (year+overall, year+round+round_pick).
- ``get_draft_order`` ordering and empty result.
- ``bulk_replace_draft_order`` idempotency + wholesale replace.
- ``get_mock_consensus_board`` join: team overlay attaches by consensus_rank,
  traded picks surface the original team, and rows beyond the seeded order
  degrade gracefully (team fields ``None``).
- The ``/consensus`` route renders the Team column only when the overlay flag
  is enabled (and the calendar is post-lottery).

Guard: integration tests require ``TEST_DATABASE_URL`` and ``PYTEST_ALLOW_DB=1``
— see ``tests/integration/conftest.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.consensus import (
    BigBoardConsensus,
    ConsensusSnapshot,
    ConsensusTrigger,
)
from app.schemas.draft_pick_slots import DraftPickSlot
from app.schemas.nba_teams import NbaTeam
from app.schemas.players_master import PlayerMaster
from app.services import consensus_read_service as read_svc
from app.services.draft_order_service import (
    PickSlotInput,
    bulk_replace_draft_order,
    get_draft_order,
)
from tests.integration.conftest import make_player

DRAFT_YEAR = 2026


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_team(db: AsyncSession, *, abbr: str, name: str) -> NbaTeam:
    team = NbaTeam(
        name=name,
        abbreviation=abbr,
        slug=name.lower().replace(" ", "-"),
        logo_url=f"https://cdn.example.com/{abbr.lower()}.png",
        primary_color="#123456",
    )
    db.add(team)
    await db.flush()
    return team


async def _make_consensus(
    db: AsyncSession,
    *,
    rows: list[tuple[PlayerMaster, int]],  # (player, consensus_rank)
) -> ConsensusSnapshot:
    """Insert a snapshot + BigBoardConsensus rows directly (read-path only)."""
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
    for player, rank in rows:
        assert player.id is not None
        db.add(
            BigBoardConsensus(
                snapshot_id=snap.id,
                draft_year=DRAFT_YEAR,
                player_id=player.id,
                consensus_rank=rank,
                avg_rank=float(rank),
                median_rank=float(rank),
                high_rank=rank,
                low_rank=rank,
                std_dev=0.0,
                num_sources=2,
            )
        )
    await db.flush()
    return snap


# ---------------------------------------------------------------------------
# Schema constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unique_year_overall_pick(db_session: AsyncSession) -> None:
    """Two slots with the same (draft_year, overall_pick) are rejected."""
    team = await _make_team(db_session, abbr="WAS", name="Washington Wizards")
    assert team.id is not None
    db_session.add(
        DraftPickSlot(
            draft_year=DRAFT_YEAR,
            overall_pick=1,
            round=1,
            round_pick=1,
            team_id=team.id,
        )
    )
    await db_session.flush()
    db_session.add(
        DraftPickSlot(
            draft_year=DRAFT_YEAR,
            overall_pick=1,
            round=1,
            round_pick=2,
            team_id=team.id,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_unique_year_round_pick(db_session: AsyncSession) -> None:
    """Two slots with the same (draft_year, round, round_pick) are rejected."""
    team = await _make_team(db_session, abbr="UTA", name="Utah Jazz")
    assert team.id is not None
    db_session.add(
        DraftPickSlot(
            draft_year=DRAFT_YEAR,
            overall_pick=1,
            round=1,
            round_pick=1,
            team_id=team.id,
        )
    )
    await db_session.flush()
    db_session.add(
        DraftPickSlot(
            draft_year=DRAFT_YEAR,
            overall_pick=2,
            round=1,
            round_pick=1,
            team_id=team.id,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Service: get_draft_order + bulk_replace_draft_order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_draft_order_empty(db_session: AsyncSession) -> None:
    """Unseeded year returns an empty list."""
    assert await get_draft_order(db_session, draft_year=DRAFT_YEAR) == []


@pytest.mark.asyncio
async def test_bulk_replace_orders_and_is_idempotent(db_session: AsyncSession) -> None:
    """bulk_replace stages an ordered set and re-running yields the same state."""
    was = await _make_team(db_session, abbr="WAS", name="Washington Wizards")
    uta = await _make_team(db_session, abbr="UTA", name="Utah Jazz")
    assert was.id is not None and uta.id is not None

    slots = [
        PickSlotInput(overall_pick=2, round=1, round_pick=2, team_id=uta.id),
        PickSlotInput(overall_pick=1, round=1, round_pick=1, team_id=was.id),
    ]
    n1 = await bulk_replace_draft_order(db_session, draft_year=DRAFT_YEAR, slots=slots)
    assert n1 == 2

    # Running again with the same input does not duplicate rows.
    n2 = await bulk_replace_draft_order(db_session, draft_year=DRAFT_YEAR, slots=slots)
    assert n2 == 2

    order = await get_draft_order(db_session, draft_year=DRAFT_YEAR)
    assert [s.overall_pick for s in order] == [1, 2]  # ordered by overall_pick
    assert len(order) == 2


@pytest.mark.asyncio
async def test_bulk_replace_swaps_wholesale(db_session: AsyncSession) -> None:
    """A second replace with fewer slots removes the surplus rows."""
    was = await _make_team(db_session, abbr="WAS", name="Washington Wizards")
    assert was.id is not None

    await bulk_replace_draft_order(
        db_session,
        draft_year=DRAFT_YEAR,
        slots=[
            PickSlotInput(overall_pick=1, round=1, round_pick=1, team_id=was.id),
            PickSlotInput(overall_pick=2, round=1, round_pick=2, team_id=was.id),
        ],
    )
    await bulk_replace_draft_order(
        db_session,
        draft_year=DRAFT_YEAR,
        slots=[PickSlotInput(overall_pick=1, round=1, round_pick=1, team_id=was.id)],
    )
    order = await get_draft_order(db_session, draft_year=DRAFT_YEAR)
    assert [s.overall_pick for s in order] == [1]


# ---------------------------------------------------------------------------
# Service: get_mock_consensus_board (the presentation join)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_board_overlays_teams_and_degrades(db_session: AsyncSession) -> None:
    """Teams overlay by rank; traded picks show origin; unseeded ranks degrade.

    consensus_rank N maps to slot N's owner, a traded pick surfaces its original
    team, and a rank past the seeded order keeps every team field ``None``.
    """
    was = await _make_team(db_session, abbr="WAS", name="Washington Wizards")
    uta = await _make_team(db_session, abbr="UTA", name="Utah Jazz")
    assert was.id is not None and uta.id is not None

    p1 = make_player("Alpha", "One", school="State")
    p2 = make_player("Bravo", "Two", school="Tech")
    p3 = make_player("Charlie", "Three", school="U")
    db_session.add_all([p1, p2, p3])
    await db_session.flush()

    await _make_consensus(db_session, rows=[(p1, 1), (p2, 2), (p3, 3)])

    # Seed only picks 1 and 2 — rank 3 has no slot (degradation case).
    await bulk_replace_draft_order(
        db_session,
        draft_year=DRAFT_YEAR,
        slots=[
            PickSlotInput(overall_pick=1, round=1, round_pick=1, team_id=was.id),
            PickSlotInput(
                overall_pick=2,
                round=1,
                round_pick=2,
                team_id=uta.id,
                original_team_id=was.id,
                trade_note="via WAS",
            ),
        ],
    )

    rows = await read_svc.get_mock_consensus_board(db_session, draft_year=DRAFT_YEAR)
    assert [r.consensus_rank for r in rows] == [1, 2, 3]

    assert rows[0].team_abbreviation == "WAS"
    assert rows[0].overall_pick == 1
    assert rows[0].original_team_abbreviation is None

    assert rows[1].team_abbreviation == "UTA"
    assert rows[1].original_team_abbreviation == "WAS"
    assert rows[1].trade_note == "via WAS"
    assert rows[1].team_logo_url is not None

    # Degradation: consensus runs deeper than the seeded order.
    assert rows[2].team_abbreviation is None
    assert rows[2].overall_pick is None


@pytest.mark.asyncio
async def test_mock_board_empty_when_no_snapshot(db_session: AsyncSession) -> None:
    """No consensus snapshot → empty list regardless of seeded order."""
    assert (
        await read_svc.get_mock_consensus_board(db_session, draft_year=DRAFT_YEAR) == []
    )


# ---------------------------------------------------------------------------
# Route: Team column gated on the overlay flag
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def _seeded_mock_board(db_session: AsyncSession) -> None:
    """Seed a snapshot + draft order so the /consensus board has team data."""
    was = await _make_team(db_session, abbr="WAS", name="Washington Wizards")
    assert was.id is not None
    p1 = make_player("Alpha", "One", school="State")
    db_session.add(p1)
    await db_session.flush()
    await _make_consensus(db_session, rows=[(p1, 1)])
    await bulk_replace_draft_order(
        db_session,
        draft_year=DRAFT_YEAR,
        slots=[PickSlotInput(overall_pick=1, round=1, round_pick=1, team_id=was.id)],
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_consensus_page_team_column_gated_off(
    app_client: AsyncClient, _seeded_mock_board: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag off, the board renders without the Team column/chip."""
    from app.config import settings

    monkeypatch.setattr(settings, "mock_draft_team_overlay_enabled", False)
    resp = await app_client.get("/consensus")
    assert resp.status_code == 200
    assert "consensus-hero__th--team" not in resp.text


@pytest.mark.asyncio
async def test_consensus_page_team_column_gated_on(
    app_client: AsyncClient, _seeded_mock_board: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag on (post-lottery), the Team column + owner chip render."""
    from app.config import settings

    monkeypatch.setattr(settings, "mock_draft_team_overlay_enabled", True)
    resp = await app_client.get("/consensus")
    assert resp.status_code == 200
    assert "consensus-hero__th--team" in resp.text
    assert "cb-team-abbr" in resp.text
    assert "WAS" in resp.text
