"""Representative dataset for per-route query-budget tests.

Query count is data-dependent: a page that loops over results fires its N+1 only
when there are rows to loop over. So the budget tests run against a deliberately
*non-empty* dataset — enough players, boards, news, podcasts, and mentions that
trending lists, consensus panels, movers, and per-player feeds all populate and
any accidental per-row query actually shows up in the count.

The fixture commits its data (the app under test reads through a separate
session bound to the same engine/schema), then yields a small handle with the
seeded player slug so the ``/players/{slug}`` route can be exercised.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.boards import Board, BoardEntry, BoardStatus
from app.schemas.consensus import ConsensusTrigger
from app.schemas.nba_teams import NbaTeam
from app.schemas.news_sources import FeedType, NewsSource
from app.schemas.player_content_mentions import (
    ContentType,
    MentionSource,
    PlayerContentMention,
)
from app.schemas.players_master import PlayerMaster
from app.schemas.summer_league import (
    SummerLeagueEdition,
    SummerLeagueGame,
    SummerLeaguePlayerGameLog,
    SummerLeagueSourcePlayer,
    SummerLeagueTeamEntry,
)
from app.schemas.summer_league_metrics import SummerLeaguePlayerSeason
from app.services import consensus_service as svc
from tests.integration.conftest import (
    make_article,
    make_player,
    make_podcast_episode,
    make_podcast_show,
)

# The home and consensus routes query this draft year (CONSENSUS_DRAFT_YEAR in
# app/routes/ui.py). Seed boards for the same year so consensus content renders.
DRAFT_YEAR = 2026


@dataclass
class SeededData:
    """Handle returned to budget tests."""

    player_slug: str
    # Summer League seed coordinates for the year/venue/team drill-down routes.
    sl_year: int = 2025
    sl_venue: str = "las_vegas"
    sl_team: str = "perf-home"
    sl_game_id: int = 0


@pytest.fixture(autouse=True)
def _disable_embedding_side_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the best-effort player-embedding background task during seeding.

    PlayerMaster has an ``after_commit`` listener that fire-and-forgets an
    embedding write to ``DATABASE_URL`` whenever an event loop is running. Under
    the async test client that task fires, and across the parametrized re-seeds
    (each preceded by ``TRUNCATE ... RESTART IDENTITY``) its late write collides
    on ``player_embeddings_pkey`` — a known source of cross-test flakiness. It is
    irrelevant to query counts, so stub it out for deterministic budget runs.
    """
    monkeypatch.setattr(
        "app.schemas.players_master._schedule_player_embedding",
        lambda snapshot: None,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_source(db: AsyncSession, name: str) -> NewsSource:
    src = NewsSource(
        name=name,
        display_name=f"{name} Display",
        feed_type=FeedType.RSS,
        feed_url=f"https://example.com/{name}/feed.xml",
        is_active=True,
        fetch_interval_minutes=30,
    )
    db.add(src)
    await db.flush()
    return src


async def _make_board(
    db: AsyncSession,
    *,
    source: NewsSource,
    entries: list[tuple[PlayerMaster, int]],
    published_offset_hours: int,
) -> None:
    """Insert one APPROVED big board with the given (player, rank) entries."""
    assert source.id is not None
    board = Board(
        news_source_id=source.id,
        draft_year=DRAFT_YEAR,
        published_at=_now() - timedelta(hours=published_offset_hours),
        size=len(entries),
        status=BoardStatus.APPROVED,
        approved_at=_now(),
    )
    db.add(board)
    await db.flush()
    assert board.id is not None
    for player, rank in entries:
        assert player.id is not None
        db.add(BoardEntry(board_id=board.id, player_id=player.id, position=rank))
    await db.flush()


async def _seed_summer_league(
    db: AsyncSession, player: PlayerMaster
) -> SummerLeagueGame:
    """Seed a populated event, player log, and published trend projection."""
    competition = SummerLeagueEdition(
        year=2025,
        league_id="15",
        venue_slug="las_vegas",
        display_name="2025 Las Vegas",
        starts_on=date(2025, 7, 1),
        ends_on=date(2025, 7, 15),
    )
    db.add(competition)
    await db.flush()
    assert competition.id is not None
    assert player.id is not None

    franchise = NbaTeam(name="Perf Home", abbreviation="PFH", slug="perf-home")
    db.add(franchise)
    await db.flush()
    home = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_team_id=franchise.id,
        nba_stats_team_id="perf-sl-home",
        raw_team_name="Perf Home",
        raw_team_abbreviation="PFH",
        team_slug="perf-home",
    )
    away = SummerLeagueTeamEntry(
        competition_id=competition.id,
        nba_stats_team_id="perf-sl-away",
        raw_team_name="Perf Away",
        raw_team_abbreviation="PFA",
        team_slug="perf-away",
    )
    db.add_all([home, away])
    await db.flush()
    assert home.id is not None and away.id is not None
    game = SummerLeagueGame(
        competition_id=competition.id,
        nba_stats_game_id="perf-sl-game",
        game_date=date(2025, 7, 5),
        home_team_entry_id=home.id,
        away_team_entry_id=away.id,
        home_score=100,
        away_score=90,
    )
    db.add(game)
    await db.flush()
    assert game.id is not None
    source_player = SummerLeagueSourcePlayer(
        nba_stats_person_id="perf-sl-person",
        raw_player_name=player.display_name or "Player",
        normalized_name=(player.display_name or "player").lower(),
        canonical_player_id=player.id,
    )
    db.add(source_player)
    await db.flush()
    db.add(
        SummerLeaguePlayerGameLog(
            competition_id=competition.id,
            game_id=game.id,
            team_entry_id=home.id,
            source_player_id=source_player.id,
            player_id=player.id,
            nba_stats_person_id=source_player.nba_stats_person_id,
            raw_player_name=player.display_name or "Player",
            minutes_seconds=1800,
            pts=20,
            reb=8,
            ast=5,
        )
    )
    db.add(
        SummerLeaguePlayerSeason(
            competition_id=competition.id,
            player_id=player.id,
            year=competition.year,
            venue_slug=competition.venue_slug,
            version=1,
            is_current=True,
            effective_day=game.game_date,
            as_of=datetime(2025, 7, 5, 23),
            published_at=datetime(2025, 7, 5, 23, 30),
            gmsc=18.0,
            ts_pct=0.61,
            bpm=4.0,
            trend_competition_bands={
                "gmsc": {"median": 12.0, "q1": 9.0, "q3": 15.0},
                "ts_pct": {"median": 0.55, "q1": 0.50, "q3": 0.60},
                "bpm": {"median": 1.0, "q1": -1.0, "q3": 3.0},
            },
        )
    )
    return game


@pytest_asyncio.fixture()
async def representative_dataset(db_session: AsyncSession) -> SeededData:
    """Seed a realistic, non-empty cross-section of the public-page data model."""
    # --- Players -----------------------------------------------------------
    players = [
        make_player("Alpha", "Prospect", school="Duke"),
        make_player("Beta", "Prospect", school="Kentucky"),
        make_player("Gamma", "Prospect", school="Kansas"),
        make_player("Delta", "Prospect", school="Gonzaga"),
        make_player("Epsilon", "Prospect", school="Houston"),
        make_player("Zeta", "Prospect", school="UConn"),
    ]
    for p in players:
        db_session.add(p)
    await db_session.flush()
    for p in players:
        await db_session.refresh(p)

    # --- Consensus: two snapshots so movers/deltas are non-empty ----------
    src1 = await _make_source(db_session, "perf-src-1")
    src2 = await _make_source(db_session, "perf-src-2")
    src3 = await _make_source(db_session, "perf-src-3")

    ordered = list(players)
    entries_v1 = [(p, i + 1) for i, p in enumerate(ordered)]
    # Reorder for the second snapshot so rank deltas are produced.
    shuffled = list(reversed(ordered))
    entries_v2 = [(p, i + 1) for i, p in enumerate(shuffled)]

    for src in (src1, src2, src3):
        await _make_board(
            db_session, source=src, entries=entries_v1, published_offset_hours=72
        )
    await db_session.commit()
    await svc.recompute_consensus(
        db_session, draft_year=DRAFT_YEAR, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()

    for src in (src1, src2, src3):
        await _make_board(
            db_session, source=src, entries=entries_v2, published_offset_hours=3
        )
    await db_session.commit()
    await svc.recompute_consensus(
        db_session, draft_year=DRAFT_YEAR, trigger=ConsensusTrigger.MANUAL
    )
    await db_session.commit()

    # --- News: feed items, several tied to players ------------------------
    news_src = await _make_source(db_session, "perf-news-src")
    assert news_src.id is not None
    news_items = []
    for i in range(8):
        target = players[i % len(players)]
        article = make_article(
            source_id=news_src.id,
            external_id=f"perf-news-{i}",
            hours_ago=i + 1,
            player_id=target.id if i % 2 == 0 else None,
            image_url="https://example.com/img.jpg" if i == 0 else None,
        )
        db_session.add(article)
        news_items.append((article, target))
    await db_session.flush()

    # Mentions so trending lists populate and per-player loops iterate.
    for article, target in news_items:
        assert article.id is not None and target.id is not None
        db_session.add(
            PlayerContentMention(
                player_id=target.id,
                content_type=ContentType.NEWS,
                content_id=article.id,
                published_at=article.published_at,
                source=MentionSource.BACKFILL,
            )
        )

    # --- Podcasts ---------------------------------------------------------
    show = make_podcast_show()
    db_session.add(show)
    await db_session.flush()
    assert show.id is not None
    for i in range(5):
        db_session.add(
            make_podcast_episode(
                show_id=show.id,
                external_id=f"perf-ep-{i}",
                hours_ago=i + 1,
                player_id=players[i % len(players)].id,
            )
        )

    # Populate the SL pages and ensure the player route exercises both trend
    # queries rather than stopping after scope resolution.
    sl_game = await _seed_summer_league(db_session, players[0])
    assert sl_game.id is not None

    await db_session.commit()

    return SeededData(
        player_slug=players[0].slug or "alpha-prospect",
        sl_game_id=sl_game.id,
    )
