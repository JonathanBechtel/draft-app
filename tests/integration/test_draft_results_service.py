"""Integration tests for the draft-recap read service.

Exercises ``get_draft_recap``, ``get_steals_and_reaches`` and
``get_source_accuracy`` against a live Postgres test schema seeded with a
consensus snapshot, actual draft results, and source boards.

Guard: integration tests require ``TEST_DATABASE_URL`` and ``PYTEST_ALLOW_DB=1``
— see ``tests/integration/conftest.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardEntry, BoardKind, BoardStatus
from app.schemas.consensus import (
    BigBoardConsensus,
    ConsensusSnapshot,
    ConsensusTrigger,
)
from app.schemas.draft_results import DraftResult
from app.schemas.nba_teams import NbaTeam
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.players_master import PlayerMaster
from app.services import draft_results_service as svc
from tests.integration.conftest import make_player

DRAFT_YEAR = 2026


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _team(db: AsyncSession, abbr: str) -> NbaTeam:
    t = NbaTeam(
        name=f"{abbr} Team",
        abbreviation=abbr,
        slug=abbr.lower(),
        logo_url=f"https://cdn.example.com/{abbr.lower()}.png",
        primary_color="#123456",
    )
    db.add(t)
    await db.flush()
    return t


async def _player(db: AsyncSession, first: str, last: str) -> PlayerMaster:
    p = make_player(first, last, school="Test U")
    db.add(p)
    await db.flush()
    return p


async def _source(db: AsyncSession, name: str) -> NewsSource:
    s = NewsSource(
        name=name,
        display_name=name.title(),
        feed_type=FeedType.RSS,
        feed_url=f"https://example.com/{name}/feed.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db.add(s)
    await db.flush()
    return s


async def _snapshot(
    db: AsyncSession, rows: list[tuple[PlayerMaster, int, int, int]]
) -> ConsensusSnapshot:
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
    for player, rank, high, low in rows:
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
                num_sources=3,
            )
        )
    await db.flush()
    return snap


async def _result(
    db: AsyncSession,
    *,
    pick: int,
    player: PlayerMaster | None,
    team: NbaTeam,
    raw_name: str = "",
) -> DraftResult:
    assert team.id is not None
    dr = DraftResult(
        draft_year=DRAFT_YEAR,
        overall_pick=pick,
        round=1 if pick <= 30 else 2,
        round_pick=pick if pick <= 30 else pick - 30,
        player_id=player.id if player else None,
        team_id=team.id,
        raw_player_name=raw_name or (player.display_name if player else ""),
        raw_team=team.abbreviation,
        resolution_method="matched" if player else "unresolved",
    )
    db.add(dr)
    await db.flush()
    return dr


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession) -> dict[str, object]:
    """Seed a small draft: two in-range, one slid (later), one jumped (earlier).

    Consensus ranges (high–low): flagg 1(1–2), harper 2(1–3), edge 3(2–5),
    reach 12(8–15). Actual picks land flagg@1 / harper@2 in range, reach@3
    (jumped, ahead of its 8–15 band), edge@12 (slid, past its 2–5 band), plus an
    unranked surprise at #4.
    """
    dal = await _team(db_session, "DAL")
    sas = await _team(db_session, "SAS")
    phi = await _team(db_session, "PHI")
    okc = await _team(db_session, "OKC")

    flagg = await _player(db_session, "Cooper", "Flagg")
    harper = await _player(db_session, "Dylan", "Harper")
    edge = await _player(db_session, "VJ", "Edgecombe")
    reach = await _player(db_session, "Reach", "Guy")
    unranked = await _player(db_session, "Wildcard", "Rookie")  # not on board

    await _snapshot(
        db_session,
        [(flagg, 1, 1, 2), (harper, 2, 1, 3), (edge, 3, 2, 5), (reach, 12, 8, 15)],
    )

    await _result(db_session, pick=1, player=flagg, team=dal)
    await _result(db_session, pick=2, player=harper, team=sas)
    await _result(db_session, pick=3, player=reach, team=phi)  # jumped
    await _result(db_session, pick=4, player=unranked, team=okc)
    await _result(db_session, pick=12, player=edge, team=dal)  # slid

    await db_session.commit()
    return {
        "flagg": flagg,
        "edge": edge,
        "reach": reach,
        "unranked": unranked,
    }


@pytest.mark.asyncio
async def test_recap_orders_by_pick_and_computes_delta(
    db_session: AsyncSession, seeded: dict[str, object]
) -> None:
    """Recap rows come back in pick order, classified by consensus range."""
    picks, summary = await svc.get_draft_recap(db_session, draft_year=DRAFT_YEAR)

    assert [p.overall_pick for p in picks] == [1, 2, 3, 4, 12]

    by_pick = {p.overall_pick: p for p in picks}
    # In range: pick 1 inside flagg's 1–2 band.
    assert by_pick[1].delta == 0
    assert by_pick[1].classification == "in_range"
    # Jumped: pick 3 ahead of reach's 8–15 band -> earlier, surprise 3-8=-5.
    assert by_pick[3].classification == "earlier"
    assert by_pick[3].range_surprise == -5
    # Unranked drafted player.
    assert by_pick[4].consensus_rank is None
    assert by_pick[4].classification == "unranked"
    # Slid: pick 12 past edge's 2–5 band -> later, surprise 12-5=7.
    assert by_pick[12].classification == "later"
    assert by_pick[12].range_surprise == 7

    assert summary.num_picks == 5
    assert summary.num_ranked == 4
    assert summary.num_unranked == 1
    assert summary.num_in_range == 2  # flagg + harper
    assert summary.biggest_later is not None
    assert summary.biggest_later.overall_pick == 12
    assert summary.biggest_earlier is not None
    assert summary.biggest_earlier.overall_pick == 3


@pytest.mark.asyncio
async def test_movers_leaderboards(
    db_session: AsyncSession, seeded: dict[str, object]
) -> None:
    """Later sorts by descending surprise, earlier ascending; in-range excluded."""
    later, earlier = await svc.get_movers(db_session, draft_year=DRAFT_YEAR, limit=10)
    assert [p.overall_pick for p in later] == [12]
    assert [p.overall_pick for p in earlier] == [3]
    # In-range chalk pick is in neither list.
    assert all(p.overall_pick != 1 for p in later + earlier)


@pytest.mark.asyncio
async def test_wide_range_pick_is_a_mover_by_point_delta(
    db_session: AsyncSession,
) -> None:
    """A big point swing reads as a riser even inside a wide consensus band.

    Mirrors the real case (Nate Ament: expected #22, range 10–38, drafted #13):
    the range test calls the pick chalk, but the nine-slot jump should still
    surface as a riser and drive the colour gradient via ``direction`` /
    ``delta_shade``. Membership is point-delta based, not range based.
    """
    mia = await _team(db_session, "MIA")
    ament = await _player(db_session, "Nate", "Ament")
    await _snapshot(db_session, [(ament, 22, 10, 38)])
    await _result(db_session, pick=13, player=ament, team=mia)
    await db_session.commit()

    picks, summary = await svc.get_draft_recap(db_session, draft_year=DRAFT_YEAR)
    p = picks[0]
    # Range-based stat still treats the wide band as chalk...
    assert p.classification == "in_range"
    # ...but the point delta makes him a nine-slot riser with a deep tint.
    assert p.delta == -9
    assert p.direction == "earlier"
    assert p.delta_shade == pytest.approx(9 / 12)
    assert summary.biggest_earlier is not None
    assert summary.biggest_earlier.player_id == ament.id

    later, earlier = svc.split_movers(picks)
    assert [pp.overall_pick for pp in earlier] == [13]
    assert later == []


@pytest.mark.asyncio
async def test_empty_when_no_results(db_session: AsyncSession) -> None:
    """With no draft_results rows the recap is empty, not an error."""
    picks, summary = await svc.get_draft_recap(db_session, draft_year=2099)
    assert picks == []
    assert summary.num_picks == 0
    assert summary.biggest_later is None


@pytest.mark.asyncio
async def test_source_accuracy_ranks_by_error(
    db_session: AsyncSession, seeded: dict[str, object]
) -> None:
    """The source whose board best matches actual order ranks first."""
    flagg = seeded["flagg"]
    edge = seeded["edge"]
    reach = seeded["reach"]
    assert isinstance(flagg, PlayerMaster)
    assert isinstance(edge, PlayerMaster)
    assert isinstance(reach, PlayerMaster)

    accurate = await _source(db_session, "sharp")
    sloppy = await _source(db_session, "noisy")

    # Accurate board predicts the actual order well.
    board_a = Board(
        news_source_id=accurate.id,
        draft_year=DRAFT_YEAR,
        published_at=_now() - timedelta(days=1),
        size=3,
        status=BoardStatus.APPROVED,
        kind=BoardKind.MOCK_DRAFT,
        num_rounds=1,
        approved_at=_now(),
    )
    db_session.add(board_a)
    await db_session.flush()
    assert board_a.id is not None
    assert flagg.id is not None and edge.id is not None and reach.id is not None
    # actual: flagg@1, reach@3, edge@12
    db_session.add(BoardEntry(board_id=board_a.id, player_id=flagg.id, position=1))
    db_session.add(BoardEntry(board_id=board_a.id, player_id=reach.id, position=4))
    db_session.add(BoardEntry(board_id=board_a.id, player_id=edge.id, position=11))

    board_b = Board(
        news_source_id=sloppy.id,
        draft_year=DRAFT_YEAR,
        published_at=_now() - timedelta(days=1),
        size=3,
        status=BoardStatus.APPROVED,
        kind=BoardKind.BIG_BOARD,
        approved_at=_now(),
    )
    db_session.add(board_b)
    await db_session.flush()
    assert board_b.id is not None
    db_session.add(BoardEntry(board_id=board_b.id, player_id=flagg.id, position=3))
    db_session.add(BoardEntry(board_id=board_b.id, player_id=reach.id, position=20))
    db_session.add(BoardEntry(board_id=board_b.id, player_id=edge.id, position=1))
    await db_session.commit()

    rows = await svc.get_source_accuracy(
        db_session, draft_year=DRAFT_YEAR, min_shared=3, include_consensus=True
    )
    # Two analyst boards + the folded-in consensus row.
    assert len(rows) == 3
    by_name = {r.source_name: r for r in rows}
    # Sharp predicted the order perfectly -> order_match 100, ranked first.
    assert rows[0].source_name == "sharp"
    assert by_name["sharp"].order_match == 100
    # Noisy inverted two picks -> lowest order_match, ranked last.
    assert rows[-1].source_name == "noisy"
    noisy_om = by_name["noisy"].order_match
    assert noisy_om is not None and noisy_om < 100
    # The consensus blend is scored and folded into the ranking.
    consensus = next(r for r in rows if r.is_consensus)
    assert consensus.source_display_name == "DraftGuru Consensus"
    assert consensus.order_match is not None
    # Sharp nailed the #1 overall pick (flagg @ position 1); noisy did not.
    assert by_name["sharp"].nailed_first_overall is True
    assert by_name["noisy"].nailed_first_overall is False
    assert by_name["sharp"].within_five == 3


@pytest.mark.asyncio
async def test_has_draft_results(
    db_session: AsyncSession, seeded: dict[str, object]
) -> None:
    """has_draft_results is True for the seeded year, False otherwise."""
    assert await svc.has_draft_results(db_session, draft_year=DRAFT_YEAR) is True
    assert await svc.has_draft_results(db_session, draft_year=2099) is False
